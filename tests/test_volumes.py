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
ISCSI_DATA = {
    "target_portal": "192.0.2.10:3260",
    "target_iqn": "iqn.2010-10.org.openstack:volume-1",
    "target_lun": 0,
    "volume_id": "vol-id",
}
DA_PATH = "/dev/da3"


def make_ci(**kwargs):
    """Return a zfs_local connection_info as the Cinder ZFS driver hands it out."""
    base = {
        "driver_volume_type": "zfs_local",
        "data": {"device_path": DEVICE_PATH, "volume_id": "vol-id"},
    }
    base.update(kwargs)
    return base


def make_iscsi_ci():
    """Return an iscsi connection_info before the host has logged in."""
    return {"driver_volume_type": "iscsi", "data": dict(ISCSI_DATA)}


def make_bdm(mount_device="/dev/vdb", **kwargs):
    """Return a block device mapping entry around make_ci."""
    return {"connection_info": make_ci(**kwargs), "mount_device": mount_device}


def make_driver():
    """Return a driver over a mocked machine and a mocked iSCSI connector."""
    drv = driver_mod.BhyveDriver(mock.Mock())
    drv._machine = mock.Mock()
    drv._iscsi = mock.Mock()
    drv._iscsi.connect_volume.return_value = {"type": "block", "path": DA_PATH}
    return drv


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
    """disk_spec consumes the local zvol and the connected iscsi handoffs."""

    def test_zfs_local_becomes_a_disk_spec(self):
        """A zfs_local connection becomes a DiskSpec."""
        spec = driver_mod.disk_spec(make_ci(), "/dev/vdb")
        self.assertEqual(
            machine.DiskSpec(device_path=DEVICE_PATH, target_dev="vdb"), spec
        )

    def test_connected_iscsi_becomes_a_disk_spec(self):
        """An iscsi connection carrying the host device becomes a DiskSpec."""
        ci = make_iscsi_ci()
        ci["data"]["device_path"] = DA_PATH
        spec = driver_mod.disk_spec(ci, "/dev/vdb")
        self.assertEqual(machine.DiskSpec(device_path=DA_PATH, target_dev="vdb"), spec)

    def test_unknown_type_is_refused_by_name(self):
        """A volume type the driver cannot connect is refused."""
        self.assertRaises(
            exception.BhyveDriverError,
            driver_mod.disk_spec,
            make_ci(driver_volume_type="rbd"),
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

    def setUp(self):
        """Build a driver over mocked machine and connector."""
        super().setUp()
        self.driver = make_driver()

    def test_maps_bdms_in_order(self):
        """Mappings become DiskSpecs in their given order."""
        info = {"block_device_mapping": [make_bdm("/dev/vdb"), make_bdm("/dev/vdc")]}
        volumes = self.driver._boot_volumes(info)
        self.assertEqual(("vdb", "vdc"), tuple(v.target_dev for v in volumes))
        self.assertEqual(
            (DEVICE_PATH, DEVICE_PATH), tuple(v.device_path for v in volumes)
        )
        self.driver._iscsi.connect_volume.assert_not_called()

    def test_iscsi_bdm_is_connected_first(self):
        """An iscsi mapping is logged in and gets the host device path."""
        bdm = {"connection_info": make_iscsi_ci(), "mount_device": "/dev/vdb"}
        volumes = self.driver._boot_volumes({"block_device_mapping": [bdm]})
        self.assertEqual(
            (machine.DiskSpec(device_path=DA_PATH, target_dev="vdb"),), volumes
        )
        self.driver._iscsi.connect_volume.assert_called_once_with(
            bdm["connection_info"]["data"]
        )

    def test_empty_and_none_give_no_volumes(self):
        """None and an empty mapping both yield no volumes."""
        self.assertEqual((), self.driver._boot_volumes(None))
        self.assertEqual((), self.driver._boot_volumes({"block_device_mapping": []}))

    def test_a_bad_bdm_raises_rather_than_being_skipped(self):
        """A mapping with an unusable connection raises."""
        info = {
            "block_device_mapping": [
                {
                    "connection_info": {"driver_volume_type": "rbd"},
                    "mount_device": "/dev/vdb",
                }
            ]
        }
        self.assertRaises(exception.BhyveDriverError, self.driver._boot_volumes, info)


class DriverAttachDetachTestCase(unittest.TestCase):
    """attach_volume/detach_volume against a mocked machine layer."""

    def setUp(self):
        """Build a driver over a mocked machine."""
        super().setUp()
        self.driver = make_driver()
        self.instance = mock.Mock(uuid=UUID)

    def _set_state(self, state):
        """Make the mocked machine report the given power state."""
        self.driver._machine.state.return_value = (state, None)

    def test_attach_refuses_a_running_instance(self):
        """Attaching to a running instance raises before any login."""
        self._set_state(power_state.RUNNING)
        self.assertRaises(
            exception.BhyveDriverError,
            self.driver.attach_volume,
            None,
            make_iscsi_ci(),
            self.instance,
            "/dev/vdb",
        )
        self.driver._machine.attach_disk.assert_not_called()
        self.driver._iscsi.connect_volume.assert_not_called()

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
        self.driver._iscsi.connect_volume.assert_not_called()

    def test_attach_iscsi_logs_in_and_records_the_device(self):
        """An iscsi attach logs in, then attaches the host device."""
        self._set_state(power_state.SHUTDOWN)
        ci = make_iscsi_ci()
        self.driver.attach_volume(None, ci, self.instance, "/dev/vdb")
        self.driver._iscsi.connect_volume.assert_called_once_with(ci["data"])
        self.assertEqual(DA_PATH, ci["data"]["device_path"])
        self.driver._machine.attach_disk.assert_called_once_with(
            UUID, machine.DiskSpec(device_path=DA_PATH, target_dev="vdb")
        )

    def test_attach_failure_logs_out_again(self):
        """A failed machine attach leaves no session behind."""
        self._set_state(power_state.SHUTDOWN)
        self.driver._machine.attach_disk.side_effect = exception.MachineError(
            uuid=UUID, reason="boom"
        )
        ci = make_iscsi_ci()
        self.assertRaises(
            exception.MachineError,
            self.driver.attach_volume,
            None,
            ci,
            self.instance,
            "/dev/vdb",
        )
        self.driver._iscsi.disconnect_volume.assert_called_once_with(ci["data"], None)

    def test_detach_never_consults_state(self):
        """Detach goes straight to the machine layer without a state check."""
        # The machine layer refuses only when the disk is actually in a
        # running domain's definition, so detaching one that was never
        # defined converges instead of stranding the volume in "detaching".
        self.driver.detach_volume(None, make_ci(), self.instance, "/dev/vdb")
        self.driver._machine.detach_disk.assert_called_once_with(UUID, "vdb")
        self.driver._machine.state.assert_not_called()
        self.driver._iscsi.disconnect_volume.assert_not_called()

    def test_detach_iscsi_logs_out_after_the_machine(self):
        """An iscsi detach removes the disk, then logs out."""
        ci = make_iscsi_ci()
        self.driver.detach_volume(None, ci, self.instance, "/dev/vdb")
        self.driver._machine.detach_disk.assert_called_once_with(UUID, "vdb")
        self.driver._iscsi.disconnect_volume.assert_called_once_with(ci["data"], None)

    def test_extend_iscsi_reprobes(self):
        """Extend asks the connector to refresh an iscsi device."""
        ci = make_iscsi_ci()
        self.driver.extend_volume(None, ci, self.instance, 2 << 30)
        self.driver._iscsi.extend_volume.assert_called_once_with(ci["data"])

    def test_extend_local_is_a_noop(self):
        """Extend of a local zvol touches nothing."""
        self.driver.extend_volume(None, make_ci(), self.instance, 2 << 30)
        self.driver._iscsi.extend_volume.assert_not_called()


class LifecycleVolumesTestCase(unittest.TestCase):
    """power_on and destroy keep remote sessions in step with the domain."""

    def setUp(self):
        """Build a driver over mocked machine and connector."""
        super().setUp()
        self.driver = make_driver()
        self.instance = mock.Mock(uuid=UUID)
        self.iscsi_bdm = {
            "connection_info": make_iscsi_ci(),
            "mount_device": "/dev/vdc",
        }

    def test_power_on_reconnects_and_reattaches_remote_volumes(self):
        """power_on logs in again and re-attaches with the current device."""
        info = {"block_device_mapping": [make_bdm("/dev/vdb"), self.iscsi_bdm]}
        self.driver.power_on(None, self.instance, None, block_device_info=info)
        self.driver._iscsi.connect_volume.assert_called_once_with(
            self.iscsi_bdm["connection_info"]["data"]
        )
        self.driver._machine.detach_disk.assert_called_once_with(UUID, "vdc")
        self.driver._machine.attach_disk.assert_called_once_with(
            UUID, machine.DiskSpec(device_path=DA_PATH, target_dev="vdc")
        )
        self.driver._machine.start.assert_called_once_with(UUID)

    def test_power_on_without_volumes_just_starts(self):
        """power_on with no mapping starts the machine."""
        self.driver.power_on(None, self.instance, None)
        self.driver._machine.start.assert_called_once_with(UUID)
        self.driver._machine.attach_disk.assert_not_called()

    def test_destroy_logs_out_of_remote_volumes(self):
        """Destroy tears the domain down, then logs out of its volumes."""
        info = {"block_device_mapping": [make_bdm("/dev/vdb"), self.iscsi_bdm]}
        with mock.patch.object(self.driver, "_teardown") as teardown:
            self.driver.destroy(None, self.instance, None, block_device_info=info)
        teardown.assert_called_once_with(UUID, destroy_disks=True)
        self.driver._iscsi.disconnect_volume.assert_called_once_with(
            self.iscsi_bdm["connection_info"]["data"], None
        )

    def test_destroy_keeps_going_when_a_logout_fails(self):
        """A failed logout during destroy is logged, not raised."""
        self.driver._iscsi.disconnect_volume.side_effect = RuntimeError("gone")
        info = {"block_device_mapping": [self.iscsi_bdm]}
        with mock.patch.object(self.driver, "_teardown"):
            with self.assertLogs(driver_mod.LOG.logger, "WARNING"):
                self.driver.destroy(None, self.instance, None, block_device_info=info)

    def test_volume_connector_reports_the_initiator(self):
        """The connector properties carry the FreeBSD initiator name."""
        with mock.patch.object(
            driver_mod.brick_iscsi.FreeBSDISCSIConnector,
            "get_connector_properties",
            return_value={"initiator": "iqn.1994-09.org.freebsd:compute1"},
        ):
            props = self.driver.get_volume_connector(self.instance)
        self.assertEqual("iqn.1994-09.org.freebsd:compute1", props["initiator"])
        self.assertEqual(CONF.host, props["host"])
        self.assertEqual("freebsd", props["os_type"])
        self.assertFalse(props["multipath"])


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
