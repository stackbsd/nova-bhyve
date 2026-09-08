"""Tests for the cold-only volume attach path."""

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
DEVICE_PATH = "/dev/zvol/zroot/cinder/volume-1"


def make_ci(**kwargs):
    """Return a zfs_local connection_info as the Cinder ZFS driver hands it out."""
    base = {
        "driver_volume_type": "zfs_local",
        "data": {"device_path": DEVICE_PATH, "volume_id": "vol-id"},
    }
    base.update(kwargs)
    return base


def make_bdm(mount_device="/dev/vdb", **kwargs):
    """Return a block device mapping entry around make_ci."""
    return {"connection_info": make_ci(**kwargs), "mount_device": mount_device}


class TargetDevTestCase(unittest.TestCase):
    """target_dev derives the guest device name from the attachment."""

    def test_mountpoint_basename(self):
        """The mountpoint's basename is the target device."""
        self.assertEqual("vdb", driver_mod.target_dev("/dev/vdb", make_ci()))

    def test_falls_back_to_connection_info_device(self):
        """Without a mountpoint, the connection_info's device is used."""
        self.assertEqual("vdc", driver_mod.target_dev(None, {"device": "/dev/vdc"}))

    def test_no_device_anywhere_is_refused(self):
        """No device name anywhere raises."""
        self.assertRaises(
            exception.BhyveDriverError, driver_mod.target_dev, None, make_ci()
        )


class DiskSpecTestCase(unittest.TestCase):
    """disk_spec consumes only the local zvol handoff."""

    def test_zfs_local_becomes_a_disk_spec(self):
        """A zfs_local connection becomes a DiskSpec."""
        spec = driver_mod.disk_spec(make_ci(), "/dev/vdb")
        self.assertEqual(
            machine.DiskSpec(device_path=DEVICE_PATH, target_dev="vdb"), spec
        )

    def test_iscsi_is_refused_by_name(self):
        """An iscsi connection is refused rather than half attached."""
        self.assertRaises(
            exception.BhyveDriverError,
            driver_mod.disk_spec,
            make_ci(driver_volume_type="iscsi"),
            "/dev/vdb",
        )

    def test_missing_device_path_is_refused(self):
        """A connection without a device_path is refused."""
        self.assertRaises(
            exception.BhyveDriverError,
            driver_mod.disk_spec,
            make_ci(data={}),
            "/dev/vdb",
        )

    def test_no_connection_info_is_refused(self):
        """A missing connection_info is refused."""
        self.assertRaises(
            exception.BhyveDriverError, driver_mod.disk_spec, None, "/dev/vdb"
        )


class BootVolumesTestCase(unittest.TestCase):
    """Volumes present at boot come from the block device mapping."""

    def test_maps_bdms_in_order(self):
        """Mappings become DiskSpecs in their given order."""
        info = {"block_device_mapping": [make_bdm("/dev/vdb"), make_bdm("/dev/vdc")]}
        volumes = driver_mod.BhyveDriver._boot_volumes(info)
        self.assertEqual(("vdb", "vdc"), tuple(v.target_dev for v in volumes))
        self.assertEqual(
            (DEVICE_PATH, DEVICE_PATH), tuple(v.device_path for v in volumes)
        )

    def test_empty_and_none_give_no_volumes(self):
        """None and an empty mapping both yield no volumes."""
        self.assertEqual((), driver_mod.BhyveDriver._boot_volumes(None))
        self.assertEqual(
            (), driver_mod.BhyveDriver._boot_volumes({"block_device_mapping": []})
        )

    def test_a_bad_bdm_raises_rather_than_being_skipped(self):
        """A mapping with an unusable connection raises."""
        info = {
            "block_device_mapping": [
                {
                    "connection_info": {"driver_volume_type": "iscsi"},
                    "mount_device": "/dev/vdb",
                }
            ]
        }
        self.assertRaises(
            exception.BhyveDriverError, driver_mod.BhyveDriver._boot_volumes, info
        )


class DriverAttachDetachTestCase(unittest.TestCase):
    """attach_volume/detach_volume against a mocked machine layer."""

    def setUp(self):
        """Build a driver over a mocked machine."""
        super().setUp()
        self.driver = driver_mod.BhyveDriver(mock.Mock())
        self.driver._machine = mock.Mock()
        self.instance = mock.Mock(uuid=UUID)

    def _set_state(self, state):
        """Make the mocked machine report the given power state."""
        self.driver._machine.state.return_value = (state, None)

    def test_attach_refuses_a_running_instance(self):
        """Attaching to a running instance raises before the machine layer."""
        self._set_state(power_state.RUNNING)
        self.assertRaises(
            exception.BhyveDriverError,
            self.driver.attach_volume,
            None,
            make_ci(),
            self.instance,
            "/dev/vdb",
        )
        self.driver._machine.attach_disk.assert_not_called()

    def test_attach_refuses_encryption(self):
        """Encrypted volumes are refused."""
        self.assertRaises(
            exception.BhyveDriverError,
            self.driver.attach_volume,
            None,
            make_ci(),
            self.instance,
            "/dev/vdb",
            encryption={"provider": "luks"},
        )
        self.driver._machine.attach_disk.assert_not_called()

    def test_attach_passes_the_disk_spec_down(self):
        """A stopped instance's attach reaches the machine layer as a DiskSpec."""
        self._set_state(power_state.SHUTDOWN)
        self.driver.attach_volume(None, make_ci(), self.instance, "/dev/vdb")
        self.driver._machine.attach_disk.assert_called_once_with(
            UUID, machine.DiskSpec(device_path=DEVICE_PATH, target_dev="vdb")
        )

    def test_detach_never_consults_state(self):
        """Detach goes straight to the machine layer without a state check."""
        # The machine layer refuses only when the disk is actually in a
        # running domain's definition, so detaching one that was never
        # defined converges instead of stranding the volume in "detaching".
        self.driver.detach_volume(None, make_ci(), self.instance, "/dev/vdb")
        self.driver._machine.detach_disk.assert_called_once_with(UUID, "vdb")
        self.driver._machine.state.assert_not_called()


class DirectMachineDiskTestCase(unittest.TestCase):
    """attach_disk/detach_disk mutate the persisted spec, cold only."""

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
        self.disk = machine.DiskSpec(device_path=DEVICE_PATH, target_dev="vdb")

    def _define(self, **kwargs):
        """Define the test instance with optional spec overrides."""
        base = dict(
            uuid=UUID,
            title="test",
            vcpus=1,
            mem_mib=512,
            root_dev="/dev/zvol/tank/nova/instances/inst",
            iso_path=None,
            volumes=(),
        )
        base.update(kwargs)
        self.machine.define(machine.MachineSpec(**base))

    def test_attach_persists_into_the_spec(self):
        """An attach lands in the persisted spec."""
        self._define()
        self.machine.attach_disk(UUID, self.disk)
        self.assertEqual((self.disk,), self.machine._read_spec(UUID).volumes)

    def test_attach_same_target_again_is_idempotent(self):
        """Attaching to an occupied target changes nothing."""
        self._define(volumes=(self.disk,))
        other = machine.DiskSpec(device_path="/dev/zvol/other", target_dev="vdb")
        self.machine.attach_disk(UUID, other)
        self.assertEqual((self.disk,), self.machine._read_spec(UUID).volumes)

    def test_attach_while_running_is_refused(self):
        """A running instance refuses the attach."""
        self._define()
        self.state.return_value = (power_state.RUNNING, None)
        self.assertRaises(
            exception.MachineError, self.machine.attach_disk, UUID, self.disk
        )

    def test_attach_to_an_undefined_domain_is_refused(self):
        """An undefined domain refuses the attach."""
        self.assertRaises(
            exception.MachineError, self.machine.attach_disk, UUID, self.disk
        )

    def test_detach_removes_and_persists(self):
        """A detach removes the disk from the persisted spec."""
        self._define(volumes=(self.disk,))
        self.machine.detach_disk(UUID, "vdb")
        self.assertEqual((), self.machine._read_spec(UUID).volumes)

    def test_detach_of_an_absent_disk_succeeds_even_while_running(self):
        """Detaching an absent disk succeeds regardless of power state."""
        # The absence check precedes the running guard: detach runs on
        # teardown and retry paths, and removing a disk that is not there
        # needs no change at all.
        self._define()
        self.state.return_value = (power_state.RUNNING, None)
        self.machine.detach_disk(UUID, "vdb")
        self.assertEqual((), self.machine._read_spec(UUID).volumes)

    def test_detach_from_an_undefined_domain_is_refused(self):
        """An undefined domain refuses the detach."""
        self.assertRaises(exception.MachineError, self.machine.detach_disk, UUID, "vdb")


if __name__ == "__main__":
    unittest.main()
