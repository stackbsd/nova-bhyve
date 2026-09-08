"""Tests for the NIC path."""

import tempfile
import unittest
from unittest import mock

import nova.conf

from nova.virt.bhyve import privsep
from nova_bhyve import driver as bhyve_driver
from nova_bhyve import exception, machine
from nova_bhyve.machine import direct

CONF = nova.conf.CONF


def make_vif(
    vif_id="3fb01977-a4e2-4a28-9a6f-1f0a3a2b4c5d",
    vif_type="bridge",
    bridge="pbd2db231d54fe",
    mac="fa:16:3e:aa:bb:cc",
):
    """Return a vif dict shaped like nova's network_info entries."""
    v = {
        "id": vif_id,
        "type": vif_type,
        "address": mac,
        "details": {"bridge_name": bridge},
    }
    if bridge is None:
        v["details"] = {}
    return v


class NicSpecsTestCase(unittest.TestCase):
    """network_info to NicSpec, the seam between neutron's world and ours."""

    def test_tap_name_follows_the_neutron_convention(self):
        """Tap names are tap + 11 chars of the port id, within IFNAMSIZ."""
        nics = bhyve_driver.nic_specs([make_vif()])
        self.assertEqual("tap3fb01977-a4", nics[0].tap_name)
        self.assertLessEqual(len(nics[0].tap_name), 15)  # IFNAMSIZ

    def test_bridge_comes_from_vif_details_verbatim(self):
        """The bridge name is taken from vif_details, never recomputed."""
        nics = bhyve_driver.nic_specs([make_vif(bridge="pb0123456789ab")])
        self.assertEqual("pb0123456789ab", nics[0].bridge)

    def test_mac_is_carried_through(self):
        """The neutron-allocated MAC is carried through unchanged."""
        nics = bhyve_driver.nic_specs([make_vif(mac="fa:16:3e:01:02:03")])
        self.assertEqual("fa:16:3e:01:02:03", nics[0].mac)

    def test_no_network_info_means_no_nics(self):
        """None and an empty list both yield no nics."""
        self.assertEqual((), bhyve_driver.nic_specs(None))
        self.assertEqual((), bhyve_driver.nic_specs([]))

    def test_a_foreign_vif_type_is_refused_by_name(self):
        """A non-bridge vif type is refused."""
        self.assertRaises(
            exception.BhyveDriverError,
            bhyve_driver.nic_specs,
            [make_vif(vif_type="ovs")],
        )

    def test_a_missing_bridge_name_is_refused(self):
        """A vif without a bridge_name in its details is refused."""
        self.assertRaises(
            exception.BhyveDriverError, bhyve_driver.nic_specs, [make_vif(bridge=None)]
        )


class NicArgvTestCase(unittest.TestCase):
    """NICs on the bhyve command line."""

    def setUp(self):
        """Configure a bootrom path and a DirectMachine under test."""
        super().setUp()
        CONF.set_override("bootrom_path", "/fw/CODE.fd", group="bhyve")
        self.addCleanup(CONF.clear_override, "bootrom_path", group="bhyve")
        self.machine = direct.DirectMachine()

    def _spec(self, **kw):
        """Return a MachineSpec with test defaults."""
        base = dict(
            uuid="11111111-2222-3333-4444-555555555555",
            title="t",
            vcpus=1,
            mem_mib=512,
            root_dev="/dev/zvol/t/r",
        )
        base.update(kw)
        return machine.MachineSpec(**base)

    def test_nic_slots_follow_the_disks(self):
        """NICs are appended after the disks without renumbering them."""
        spec = self._spec(
            volumes=(machine.DiskSpec(device_path="/dev/zvol/t/v", target_dev="vdb"),),
            nics=(
                machine.NicSpec(
                    tap_name="tap3fb01977-a4",
                    bridge="pbd2db231d54fe",
                    mac="fa:16:3e:aa:bb:cc",
                ),
            ),
        )
        argv = self.machine._build_argv(spec)
        slots = [argv[i + 1] for i, a in enumerate(argv) if a == "-s"]
        self.assertEqual(
            "4:0,virtio-net,tap3fb01977-a4,mac=fa:16:3e:aa:bb:cc", slots[-1]
        )
        self.assertIn("2:0,virtio-blk,/dev/zvol/t/r", slots)

    def test_spec_with_nics_round_trips(self):
        """A spec carrying nics persists and reads back equal."""
        with tempfile.TemporaryDirectory() as tmp:
            CONF.set_override("instances_path", tmp)
            try:
                spec = self._spec(
                    nics=(
                        machine.NicSpec(
                            tap_name="tap3fb01977-a4",
                            bridge="pbd2db231d54fe",
                            mac="fa:16:3e:aa:bb:cc",
                        ),
                    )
                )
                self.machine._write_spec(spec)
                self.assertEqual(spec, self.machine._read_spec(spec.uuid))
            finally:
                CONF.clear_override("instances_path")


class AwaitNicsTestCase(unittest.TestCase):
    """spawn waits for the networking agent's tap; it never creates one."""

    def _machine_and_spec(self):
        """Return a DirectMachine and a spec with one nic."""
        m = direct.DirectMachine()
        spec = machine.MachineSpec(
            uuid="11111111-2222-3333-4444-555555555555",
            title="t",
            vcpus=1,
            mem_mib=512,
            root_dev="/dev/zvol/t/r",
            nics=(
                machine.NicSpec(
                    tap_name="tap3fb01977-a4",
                    bridge="pb0123456789ab",
                    mac="fa:16:3e:aa:bb:cc",
                ),
            ),
        )
        return m, spec

    def test_present_tap_returns_immediately(self):
        """An existing tap needs no waiting."""
        m, spec = self._machine_and_spec()
        with mock.patch.object(
            direct.DirectMachine, "_nic_present", staticmethod(lambda _t: True)
        ):
            m._await_nics(spec)  # no exception, no sleep

    def test_absent_tap_raises_a_legible_error(self):
        """A tap that never appears raises an error naming it."""
        m, spec = self._machine_and_spec()
        m.NIC_WAIT_SECONDS = 0
        with mock.patch.object(
            direct.DirectMachine, "_nic_present", staticmethod(lambda _t: False)
        ):
            try:
                m._await_nics(spec)
                self.fail("should have raised")
            except Exception as err:
                self.assertIn("never appeared", str(err))
                self.assertIn("tap3fb01977-a4", str(err))

    def test_the_privsep_surface_is_four_functions(self):
        """Tap lifecycle is the networking layer's; privsep stays four entrypoints."""
        import inspect

        source = inspect.getsource(privsep)
        self.assertEqual(4, source.count("sys_admin_pctxt.entrypoint"))
        self.assertNotIn("def plug_vif", source)
