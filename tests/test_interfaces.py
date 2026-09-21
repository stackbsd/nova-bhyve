"""Tests for the cold-only interface attach path."""

import tempfile
import unittest
from unittest import mock

import nova.conf
from nova.compute import power_state

from nova_bhyve import driver as driver_mod
from nova_bhyve import exception, machine
from nova_bhyve.machine import direct

CONF = nova.conf.CONF

UUID = "11111111-2222-3333-4444-555555555555"
PORT_ID = "3fb01977-a4e2-4a28-9a6f-1f0a3a2b4c5d"
TAP_NAME = "tap3fb01977-a4"
BRIDGE = "pbd2db231d54fe"
MAC = "fa:16:3e:aa:bb:cc"


def make_vif(**kwargs):
    """Return a bound bridge vif shaped like nova's network_info entries."""
    base = {
        "id": PORT_ID,
        "type": "bridge",
        "address": MAC,
        "details": {"bridge_name": BRIDGE},
    }
    base.update(kwargs)
    return base


class DriverAttachDetachTestCase(unittest.TestCase):
    """attach_interface/detach_interface against a mocked machine layer."""

    def setUp(self):
        """Build a driver over a mocked machine."""
        super().setUp()
        self.driver = driver_mod.BhyveDriver(mock.Mock())
        self.driver._machine = mock.Mock()
        self.instance = mock.Mock(uuid=UUID)

    def _set_state(self, state):
        """Make the mocked machine report the given power state."""
        self.driver._machine.state.return_value = (state, None)

    def test_capability_is_advertised(self):
        """The compute manager is told interface attach is available."""
        self.assertTrue(self.driver.capabilities["supports_attach_interface"])

    def test_attach_refuses_a_running_instance(self):
        """Attaching to a running instance raises before the machine layer."""
        self._set_state(power_state.RUNNING)
        self.assertRaises(
            exception.BhyveDriverError,
            self.driver.attach_interface,
            None,
            self.instance,
            None,
            make_vif(),
        )
        self.driver._machine.attach_nic.assert_not_called()

    def test_attach_refuses_a_foreign_vif_type(self):
        """A non-bridge vif never reaches the machine layer."""
        self._set_state(power_state.SHUTDOWN)
        self.assertRaises(
            exception.BhyveDriverError,
            self.driver.attach_interface,
            None,
            self.instance,
            None,
            make_vif(type="ovs"),
        )
        self.driver._machine.attach_nic.assert_not_called()

    def test_attach_passes_the_nic_spec_down(self):
        """A stopped instance's attach reaches the machine layer as a NicSpec."""
        self._set_state(power_state.SHUTDOWN)
        self.driver.attach_interface(None, self.instance, None, make_vif())
        self.driver._machine.attach_nic.assert_called_once_with(
            UUID, machine.NicSpec(tap_name=TAP_NAME, bridge=BRIDGE, mac=MAC)
        )

    def test_detach_never_consults_state(self):
        """Detach goes straight to the machine layer without a state check."""
        self.driver.detach_interface(None, self.instance, make_vif())
        self.driver._machine.detach_nic.assert_called_once_with(UUID, TAP_NAME)
        self.driver._machine.state.assert_not_called()

    def test_detach_accepts_an_unbound_vif(self):
        """A vif that lost its binding details still detaches."""
        self.driver.detach_interface(
            None, self.instance, make_vif(type="unbound", details={})
        )
        self.driver._machine.detach_nic.assert_called_once_with(UUID, TAP_NAME)


class DirectMachineNicTestCase(unittest.TestCase):
    """attach_nic/detach_nic mutate the persisted spec, cold only."""

    def setUp(self):
        """Configure a temporary instances path and a stopped machine."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.machine = direct.DirectMachine()
        self.state = mock.patch.object(
            direct.DirectMachine, "state", return_value=(power_state.SHUTDOWN, None)
        ).start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(direct.shutil, "copyfile").start()
        self.nic = machine.NicSpec(tap_name=TAP_NAME, bridge=BRIDGE, mac=MAC)
        self.other = machine.NicSpec(
            tap_name="tap0a1b2c3d-4e", bridge=BRIDGE, mac="fa:16:3e:01:02:03"
        )

    def _define(self, **kwargs):
        """Define the test instance with optional spec overrides."""
        base = dict(
            uuid=UUID,
            title="test",
            vcpus=1,
            mem_mib=512,
            root_dev="/dev/zvol/tank/nova/instances/inst",
        )
        base.update(kwargs)
        self.machine.define(machine.MachineSpec(**base))

    def test_attach_persists_after_the_existing_nics(self):
        """An attach is appended to the persisted spec."""
        self._define(nics=(self.other,))
        self.machine.attach_nic(UUID, self.nic)
        self.assertEqual((self.other, self.nic), self.machine._read_spec(UUID).nics)

    def test_attach_same_tap_again_is_idempotent(self):
        """Attaching a tap the spec already has changes nothing."""
        self._define(nics=(self.nic,))
        again = machine.NicSpec(tap_name=TAP_NAME, bridge="pbother", mac=MAC)
        self.machine.attach_nic(UUID, again)
        self.assertEqual((self.nic,), self.machine._read_spec(UUID).nics)

    def test_attach_while_running_is_refused(self):
        """A running instance refuses the attach."""
        self._define()
        self.state.return_value = (power_state.RUNNING, None)
        self.assertRaises(
            exception.MachineError, self.machine.attach_nic, UUID, self.nic
        )
        self.assertEqual((), self.machine._read_spec(UUID).nics)

    def test_attach_to_an_undefined_domain_is_refused(self):
        """An undefined domain refuses the attach."""
        self.assertRaises(
            exception.MachineError, self.machine.attach_nic, UUID, self.nic
        )

    def test_attached_nic_reaches_the_command_line(self):
        """An attached nic is rendered into the bhyve argv."""
        self._define()
        self.machine.attach_nic(UUID, self.nic)
        argv = self.machine._build_argv(self.machine._read_spec(UUID))
        self.assertIn("3:0,virtio-net,%s,mac=%s" % (TAP_NAME, MAC), argv)

    def test_detach_removes_only_the_named_nic(self):
        """A detach removes one nic from the persisted spec."""
        self._define(nics=(self.other, self.nic))
        self.machine.detach_nic(UUID, TAP_NAME)
        self.assertEqual((self.other,), self.machine._read_spec(UUID).nics)

    def test_detach_while_running_is_refused(self):
        """A running instance refuses the detach of a nic it has."""
        self._define(nics=(self.nic,))
        self.state.return_value = (power_state.RUNNING, None)
        self.assertRaises(
            exception.MachineError, self.machine.detach_nic, UUID, TAP_NAME
        )
        self.assertEqual((self.nic,), self.machine._read_spec(UUID).nics)

    def test_detach_of_an_absent_nic_succeeds_even_while_running(self):
        """Detaching an absent nic succeeds regardless of power state."""
        self._define()
        self.state.return_value = (power_state.RUNNING, None)
        self.machine.detach_nic(UUID, TAP_NAME)
        self.assertEqual((), self.machine._read_spec(UUID).nics)

    def test_detach_from_an_undefined_domain_is_refused(self):
        """An undefined domain refuses the detach."""
        self.assertRaises(
            exception.MachineError, self.machine.detach_nic, UUID, TAP_NAME
        )


if __name__ == "__main__":
    unittest.main()
