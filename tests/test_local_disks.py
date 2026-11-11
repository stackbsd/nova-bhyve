"""Tests for ephemeral and swap disks."""

import json
import tempfile
import unittest
from unittest import mock

import nova.conf
from nova.compute import power_state
from oslo_utils import units

from nova import exception as nova_exception
from nova_bhyve import driver as driver_mod
from nova_bhyve import exception, machine
from nova_bhyve.machine import direct
from nova_bhyve.storage import zfs

CONF = nova.conf.CONF

UUID = "11111111-2222-3333-4444-555555555555"
OTHER = "66666666-7777-8888-9999-000000000000"
ROOT = "tank/nova/instances/" + UUID
EPH0 = ROOT + ".disk.eph0"
EPH1 = ROOT + ".disk.eph1"
SWAP = ROOT + ".disk.swap"


def make_bdi(ephemerals=(), swap_mb=0, swap_dev="/dev/vdd"):
    """Return block_device_info with the given ephemerals and swap."""
    return {
        "ephemerals": [
            {"device_name": dev, "size": size_gb} for dev, size_gb in ephemerals
        ],
        "swap": {"device_name": swap_dev, "swap_size": swap_mb},
        "block_device_mapping": [],
    }


def make_instance(root_gb=10, ephemeral_gb=0, swap=0):
    """Return an instance with a flavor of the given disk sizes."""
    flavor = mock.Mock(
        root_gb=root_gb, ephemeral_gb=ephemeral_gb, swap=swap, vcpus=1, memory_mb=512
    )
    return mock.Mock(uuid=UUID, display_name="test", flavor=flavor, host=CONF.host)


class FakeZFS:
    """In-memory stand-in for the zfs commands the driver runs."""

    def __init__(self):
        """Start with no zvols and no snapshots."""
        self.volumes = {}
        self.snapshots = {}

    def install(self):
        """Patch the zfs module to use this fake."""
        for name in (
            "exists",
            "get_volumes",
            "volsize_bytes",
            "set_volsize",
            "create_volume",
            "snapshot",
            "snapshots_of",
            "clones_of",
            "rollback",
            "destroy_deferred",
            "destroy_volume",
        ):
            mock.patch.object(zfs, name, getattr(self, name)).start()

    def exists(self, name):
        """Report whether a zvol or snapshot exists."""
        return name in self.volumes or name in self.snapshots

    def get_volumes(self, parent):
        """Return the zvols directly under a dataset."""
        return [v for v in self.volumes if v.rsplit("/", 1)[0] == parent]

    def volsize_bytes(self, dataset):
        """Return the size of a zvol."""
        return self.volumes[dataset]

    def set_volsize(self, dataset, size_bytes):
        """Resize a zvol."""
        self.volumes[dataset] = zfs.align_volsize(size_bytes)

    def create_volume(self, dataset, size_bytes, sparse=True):
        """Create a zvol."""
        self.volumes[dataset] = zfs.align_volsize(size_bytes)

    def snapshot(self, dataset, name):
        """Snapshot a zvol."""
        self.snapshots["%s@%s" % (dataset, name)] = self.volumes[dataset]

    def snapshots_of(self, dataset):
        """Return the snapshots of a zvol."""
        return [s for s in self.snapshots if s.split("@")[0] == dataset]

    def clones_of(self, snapshot_name):
        """Return no clones."""
        return []

    def rollback(self, dataset, name):
        """Restore a zvol's size from its snapshot."""
        self.volumes[dataset] = self.snapshots["%s@%s" % (dataset, name)]

    def destroy_deferred(self, snapshot_name):
        """Remove a snapshot."""
        self.snapshots.pop(snapshot_name, None)

    def destroy_volume(self, dataset):
        """Remove a zvol, refusing while it has snapshots."""
        if self.snapshots_of(dataset):
            raise exception.ZFSCommandError(
                command="destroy", dataset=dataset, reason="has children"
            )
        self.volumes.pop(dataset, None)


class LocalDiskTestCase(unittest.TestCase):
    """Base with a fake zfs and a driver over a mocked machine."""

    def setUp(self):
        """Configure the dataset, a fake zfs and a driver."""
        super().setUp()
        CONF.set_override("zfs_dataset", "tank/nova", group="bhyve")
        self.addCleanup(CONF.clear_override, "zfs_dataset", group="bhyve")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.zfs = FakeZFS()
        self.zfs.install()
        self.addCleanup(mock.patch.stopall)
        self.driver = driver_mod.BhyveDriver(mock.Mock())
        self.driver._machine = mock.Mock()
        self.driver._machine.state.return_value = (power_state.RUNNING, "")


class DatasetNamingTestCase(LocalDiskTestCase):
    """Local disk zvols sit next to the root zvol."""

    def test_dataset_name(self):
        """A local disk is named after the instance and the disk."""
        self.assertEqual(SWAP, zfs.local_disk_dataset(UUID, "swap"))

    def test_local_disks_skips_root_clones_and_other_instances(self):
        """Only this instance's ephemeral and swap zvols are listed."""
        for dataset in (
            ROOT,
            EPH0,
            SWAP,
            ROOT + ".nova-snapshot-image",
            "tank/nova/instances/%s.disk.eph0" % OTHER,
        ):
            self.zfs.volumes[dataset] = units.Gi
        self.assertEqual([EPH0, SWAP], sorted(zfs.local_disks(UUID)))


class WantedLocalDisksTestCase(unittest.TestCase):
    """The ephemeral and swap disks an instance should have."""

    def test_ephemerals_then_swap(self):
        """Ephemerals come in mapping order, then swap."""
        bdi = make_bdi([("/dev/vdb", 1), ("/dev/vdc", 2)], swap_mb=512)
        self.assertEqual(
            [
                ("eph0", units.Gi, "vdb"),
                ("eph1", 2 * units.Gi, "vdc"),
                ("swap", 512 * units.Mi, "vdd"),
            ],
            driver_mod.wanted_local_disks(make_instance(), bdi),
        )

    def test_nothing_requested(self):
        """No ephemeral and no swap gives no local disks."""
        self.assertEqual([], driver_mod.wanted_local_disks(make_instance(), make_bdi()))
        self.assertEqual([], driver_mod.wanted_local_disks(make_instance(), None))

    def test_flavor_ephemeral_without_a_mapping(self):
        """A flavor ephemeral_gb with no ephemeral mapping gives one disk."""
        self.assertEqual(
            [("eph0", 3 * units.Gi, "eph0")],
            driver_mod.wanted_local_disks(make_instance(ephemeral_gb=3), make_bdi()),
        )

    def test_mappings_win_over_the_flavor(self):
        """Ephemeral mappings replace the flavor ephemeral_gb."""
        bdi = make_bdi([("/dev/vdb", 1)])
        self.assertEqual(
            [("eph0", units.Gi, "vdb")],
            driver_mod.wanted_local_disks(make_instance(ephemeral_gb=3), bdi),
        )

    def test_swap_without_a_device_name(self):
        """A swap mapping added by a resize has no device name yet."""
        bdi = make_bdi(swap_mb=256, swap_dev=None)
        self.assertEqual(
            [("swap", 256 * units.Mi, "swap")],
            driver_mod.wanted_local_disks(make_instance(), bdi),
        )


class SyncLocalDisksTestCase(LocalDiskTestCase):
    """The zvols follow the wanted local disks."""

    def sync(self, bdi, instance=None):
        """Sync the test instance against a block_device_info."""
        self.driver._sync_local_disks(instance or make_instance(), bdi)

    def test_creates_missing_disks(self):
        """Every wanted disk is created at its size."""
        self.sync(make_bdi([("/dev/vdb", 1)], swap_mb=512))
        self.assertEqual({EPH0: units.Gi, SWAP: 512 * units.Mi}, self.zfs.volumes)

    def test_keeps_matching_disks(self):
        """A disk already at its size is left alone."""
        self.zfs.volumes[EPH0] = units.Gi
        with mock.patch.object(zfs, "create_volume") as create:
            self.sync(make_bdi([("/dev/vdb", 1)]))
        create.assert_not_called()
        self.assertEqual({EPH0: units.Gi}, self.zfs.volumes)

    def test_removes_unwanted_disks(self):
        """A disk no longer wanted is destroyed with its snapshots."""
        self.zfs.volumes.update({EPH0: units.Gi, SWAP: units.Gi})
        self.zfs.snapshot(SWAP, "nova-resize")
        self.sync(make_bdi([("/dev/vdb", 1)]))
        self.assertEqual({EPH0: units.Gi}, self.zfs.volumes)
        self.assertEqual({}, self.zfs.snapshots)

    def test_recreates_swap_of_another_size(self):
        """Swap of the wrong size is replaced, even when larger."""
        self.zfs.volumes[SWAP] = units.Gi
        destroy = mock.Mock(wraps=self.zfs.destroy_volume)
        with mock.patch.object(zfs, "destroy_volume", destroy):
            self.sync(make_bdi(swap_mb=512))
        destroy.assert_called_once_with(SWAP)
        self.assertEqual({SWAP: 512 * units.Mi}, self.zfs.volumes)

    def test_grows_a_smaller_ephemeral(self):
        """An ephemeral smaller than wanted is grown in place."""
        self.zfs.volumes[EPH0] = units.Gi
        self.sync(make_bdi(), instance=make_instance(ephemeral_gb=2))
        self.assertEqual({EPH0: 2 * units.Gi}, self.zfs.volumes)

    def test_never_shrinks_an_ephemeral(self):
        """An ephemeral larger than wanted keeps its size."""
        self.zfs.volumes[EPH0] = 2 * units.Gi
        self.sync(make_bdi([("/dev/vdb", 1)]))
        self.assertEqual({EPH0: 2 * units.Gi}, self.zfs.volumes)


class DefineDomainTestCase(LocalDiskTestCase):
    """The domain carries the local disks."""

    def test_local_disks_in_the_spec(self):
        """Local disks reach the machine layer as DiskSpecs."""
        bdi = make_bdi([("/dev/vdb", 1)], swap_mb=512)
        self.driver._define_domain(make_instance(), bdi)
        spec = self.driver._machine.define.call_args.args[0]
        self.assertEqual(
            (
                machine.DiskSpec(device_path="/dev/zvol/" + EPH0, target_dev="vdb"),
                machine.DiskSpec(device_path="/dev/zvol/" + SWAP, target_dev="vdd"),
            ),
            spec.local_disks,
        )


class TeardownTestCase(LocalDiskTestCase):
    """Destroying an instance removes its local disks."""

    def test_destroy_removes_every_zvol(self):
        """Root, ephemeral and swap zvols and their snapshots all go."""
        self.zfs.volumes.update({ROOT: units.Gi, EPH0: units.Gi, SWAP: units.Gi})
        self.zfs.snapshot(EPH0, "nova-resize")
        other = "tank/nova/instances/%s.disk.eph0" % OTHER
        self.zfs.volumes[other] = units.Gi
        self.driver.destroy(None, make_instance(), None)
        self.assertEqual({other: units.Gi}, self.zfs.volumes)
        self.assertEqual({}, self.zfs.snapshots)

    def test_keep_disks(self):
        """destroy_disks=False leaves every zvol in place."""
        self.zfs.volumes.update({ROOT: units.Gi, EPH0: units.Gi})
        self.driver.destroy(None, make_instance(), None, destroy_disks=False)
        self.assertEqual({ROOT: units.Gi, EPH0: units.Gi}, self.zfs.volumes)


class ResizeTestCase(LocalDiskTestCase):
    """Same-host resize with ephemeral and swap disks."""

    def setUp(self):
        """Give the instance a root, an ephemeral and swap."""
        super().setUp()
        self.zfs.volumes.update(
            {ROOT: 10 * units.Gi, EPH0: units.Gi, SWAP: 512 * units.Mi}
        )
        self.bdi = make_bdi([("/dev/vdb", 1)], swap_mb=512)

    def migrate(self, ephemeral_gb=1, root_gb=10):
        """Run migrate_disk_and_power_off onto a flavor of the given sizes."""
        flavor = mock.Mock(root_gb=root_gb, ephemeral_gb=ephemeral_gb)
        return self.driver.migrate_disk_and_power_off(
            None,
            make_instance(ephemeral_gb=1),
            CONF.my_ip,
            flavor,
            None,
            block_device_info=self.bdi,
        )

    def test_refuses_ephemeral_shrink_before_stopping(self):
        """A smaller ephemeral_gb is refused while the guest still runs."""
        self.assertRaises(
            nova_exception.InstanceFaultRollback, self.migrate, ephemeral_gb=0
        )
        self.driver._machine.shutdown.assert_not_called()
        self.assertEqual({}, self.zfs.snapshots)

    def test_snapshots_every_zvol(self):
        """Every zvol of the instance gets a revert point."""
        info = json.loads(self.migrate(ephemeral_gb=2))
        self.assertEqual(ROOT + "@nova-resize", info["revert_snapshot"])
        self.assertEqual(
            {ROOT + "@nova-resize", EPH0 + "@nova-resize", SWAP + "@nova-resize"},
            set(self.zfs.snapshots),
        )

    def test_finish_recreates_swap_at_the_new_size(self):
        """finish_migration applies the new swap size."""
        self.migrate()
        self.bdi["swap"]["swap_size"] = 1024
        self.driver.finish_migration(
            None, None, make_instance(), "{}", None, None, True, None, self.bdi
        )
        self.assertEqual(units.Gi, self.zfs.volumes[SWAP])
        self.assertNotIn(SWAP + "@nova-resize", self.zfs.snapshots)
        self.driver._machine.start.assert_called_once_with(UUID)

    def test_revert_restores_every_zvol(self):
        """A revert rolls back what it can and remakes swap at the old size."""
        self.migrate(root_gb=20)
        self.bdi["swap"]["swap_size"] = 1024
        self.driver.finish_migration(
            None,
            None,
            make_instance(root_gb=20),
            "{}",
            None,
            None,
            True,
            None,
            self.bdi,
        )
        self.zfs.volumes[EPH0] = 2 * units.Gi
        self.bdi["swap"]["swap_size"] = 512
        self.driver.finish_revert_migration(
            None, make_instance(), None, None, block_device_info=self.bdi
        )
        self.assertEqual(
            {ROOT: 10 * units.Gi, EPH0: units.Gi, SWAP: 512 * units.Mi},
            self.zfs.volumes,
        )
        self.assertEqual({}, self.zfs.snapshots)

    def test_revert_removes_swap_added_by_the_resize(self):
        """Swap the resize added is gone after a revert."""
        del self.zfs.volumes[SWAP]
        self.bdi["swap"]["swap_size"] = 0
        self.migrate()
        self.bdi["swap"]["swap_size"] = 256
        self.driver.finish_migration(
            None, None, make_instance(), "{}", None, None, True, None, self.bdi
        )
        self.assertIn(SWAP, self.zfs.volumes)
        self.bdi["swap"]["swap_size"] = 0
        self.driver.finish_revert_migration(
            None, make_instance(), None, None, block_device_info=self.bdi
        )
        self.assertNotIn(SWAP, self.zfs.volumes)

    def test_confirm_drops_every_revert_point(self):
        """Confirming the resize removes every resize snapshot."""
        self.migrate()
        self.driver.confirm_migration(None, None, make_instance(), None)
        self.assertEqual({}, self.zfs.snapshots)


class DirectMachineLocalDisksTestCase(unittest.TestCase):
    """The direct mechanism persists and boots local disks."""

    def setUp(self):
        """Configure a temporary instances path."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.machine = direct.DirectMachine()
        self.local = machine.DiskSpec(device_path="/dev/zvol/eph0", target_dev="vdb")
        self.volume = machine.DiskSpec(device_path="/dev/zvol/vol", target_dev="vdc")
        self.spec = machine.MachineSpec(
            uuid=UUID,
            title="test",
            vcpus=1,
            mem_mib=512,
            root_dev="/dev/zvol/root",
            local_disks=(self.local,),
            volumes=(self.volume,),
        )

    def test_local_disks_sit_between_root_and_volumes(self):
        """Slot order is root, local disks, then volumes."""
        argv = self.machine._build_argv(self.spec)
        slots = [argv[i + 1] for i, a in enumerate(argv) if a == "-s"]
        self.assertEqual(
            [
                "2:0,virtio-blk,/dev/zvol/root",
                "3:0,virtio-blk,/dev/zvol/eph0",
                "4:0,virtio-blk,/dev/zvol/vol",
            ],
            slots[2:],
        )

    def test_spec_round_trip(self):
        """Local disks survive a write and read of the spec."""
        self.machine._write_spec(self.spec)
        self.assertEqual(self.spec, self.machine._read_spec(UUID))

    def test_spec_written_before_local_disks_loads(self):
        """A spec file without local_disks loads with none."""
        self.machine._write_spec(self.spec)
        path = "%s/%s/%s" % (self.tmp.name, UUID, direct.SPEC_FILE)
        with open(path) as handle:
            raw = json.load(handle)
        del raw["local_disks"]
        with open(path, "w") as handle:
            json.dump(raw, handle)
        self.assertEqual((), self.machine._read_spec(UUID).local_disks)


if __name__ == "__main__":
    unittest.main()
