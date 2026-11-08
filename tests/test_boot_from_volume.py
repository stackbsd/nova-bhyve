"""Tests for instances whose root disk is a Cinder volume."""

import json
from unittest import mock

import nova.conf
from oslo_utils import units

from nova import objects
from nova_bhyve import driver as driver_mod
from nova_bhyve import images
from tests.test_local_disks import (
    EPH0,
    ROOT,
    UUID,
    LocalDiskTestCase,
    make_bdi,
    make_instance,
)
from tests.test_volumes import DA_PATH, DEVICE_PATH, make_bdm, make_iscsi_ci

CONF = nova.conf.CONF

objects.register_all()


def make_volume_bdi(root=None, others=()):
    """Return block_device_info booting from a volume at vda."""
    root = root or make_bdm("/dev/vda")
    root["boot_index"] = 0
    bdi = make_bdi()
    bdi["root_device_name"] = "/dev/vda"
    bdi["block_device_mapping"] = [root, *others]
    return bdi


class RootVolumeTestCase(LocalDiskTestCase):
    """The mapping with boot index 0 is the root disk."""

    def test_finds_the_boot_index_zero_mapping(self):
        """The root is the mapping with boot_index 0."""
        bdi = make_volume_bdi(others=[make_bdm("/dev/vdb")])
        self.assertIs(bdi["block_device_mapping"][0], driver_mod.root_volume(bdi))

    def test_image_backed_has_no_root_volume(self):
        """Without a boot_index 0 mapping there is no root volume."""
        self.assertIsNone(driver_mod.root_volume(make_bdi()))
        self.assertIsNone(driver_mod.root_volume(None))

    def test_boot_volumes_leaves_out_the_root(self):
        """The root volume is not defined a second time as a data disk."""
        bdi = make_volume_bdi(others=[make_bdm("/dev/vdb")])
        volumes = self.driver._boot_volumes(bdi)
        self.assertEqual(("vdb",), tuple(v.target_dev for v in volumes))


class SpawnFromVolumeTestCase(LocalDiskTestCase):
    """spawn boots a volume-backed instance from its root volume."""

    def setUp(self):
        """Patch the image cache and config drive out of spawn."""
        super().setUp()
        self.ensure_cached = mock.patch.object(images, "ensure_cached").start()
        self.clone = mock.patch.object(driver_mod.zfs, "clone").start()
        mock.patch.object(
            driver_mod.configdrive, "required_by", return_value=False
        ).start()

    def spawn(self, bdi):
        """Spawn with image metadata as Nova builds it from a volume."""
        image_meta = objects.ImageMeta.from_dict({"properties": {}})
        self.driver.spawn(None, make_instance(), image_meta, [], None, {}, [], bdi)
        return self.driver._machine.define.call_args.args[0]

    def test_boots_from_a_local_volume_without_an_image(self):
        """A zfs_local root is the root device and no zvol is cloned."""
        spec = self.spawn(make_volume_bdi(others=[make_bdm("/dev/vdb")]))
        self.assertEqual(DEVICE_PATH, spec.root_dev)
        self.assertEqual(("vdb",), tuple(v.target_dev for v in spec.volumes))
        self.ensure_cached.assert_not_called()
        self.clone.assert_not_called()
        self.assertNotIn(ROOT, self.zfs.volumes)
        self.driver._machine.start.assert_called_once_with(UUID)

    def test_boots_from_an_iscsi_volume(self):
        """An iscsi root is logged in and its da device is the root."""
        self.driver._iscsi = mock.Mock()
        self.driver._iscsi.connect_volume.return_value = {"path": DA_PATH}
        root = {"connection_info": make_iscsi_ci(), "mount_device": "/dev/vda"}
        spec = self.spawn(make_volume_bdi(root))
        self.assertEqual(DA_PATH, spec.root_dev)
        self.assertEqual((), spec.volumes)

    def test_power_on_redefines_with_the_current_root_device(self):
        """power_on logs in again and points the root at the new device."""
        self.driver._iscsi = mock.Mock()
        self.driver._iscsi.connect_volume.return_value = {"path": DA_PATH}
        root = {"connection_info": make_iscsi_ci(), "mount_device": "/dev/vda"}
        self.driver.power_on(
            None, make_instance(), [], block_device_info=make_volume_bdi(root)
        )
        spec = self.driver._machine.define.call_args.args[0]
        self.assertEqual(DA_PATH, spec.root_dev)
        self.driver._machine.attach_disk.assert_not_called()
        self.driver._machine.start.assert_called_once_with(UUID)


class VolumeBackedResizeTestCase(LocalDiskTestCase):
    """Resize and destroy of a volume-backed instance leave the root alone."""

    def setUp(self):
        """Give the instance an ephemeral disk and no root zvol."""
        super().setUp()
        self.zfs.volumes[EPH0] = units.Gi
        self.bdi = make_volume_bdi()
        self.bdi["ephemerals"] = [{"device_name": "/dev/vdb", "size": 1}]

    def test_resize_snapshots_only_local_disks(self):
        """Only the ephemeral zvol gets a revert point."""
        flavor = mock.Mock(root_gb=20, ephemeral_gb=1)
        info = self.driver.migrate_disk_and_power_off(
            None, make_instance(ephemeral_gb=1), CONF.my_ip, flavor, None, self.bdi
        )
        self.assertEqual(0, json.loads(info)["root_volsize_bytes"])
        self.assertEqual({EPH0 + "@nova-resize"}, set(self.zfs.snapshots))
        self.driver.finish_migration(
            None, None, make_instance(20), "{}", None, None, True, None, self.bdi
        )
        self.assertNotIn(ROOT, self.zfs.volumes)
        spec = self.driver._machine.define.call_args.args[0]
        self.assertEqual(DEVICE_PATH, spec.root_dev)

    def test_destroy_removes_local_disks(self):
        """Destroy removes the ephemeral zvol and has no root to remove."""
        self.driver.destroy(None, make_instance(), None, self.bdi)
        self.assertEqual({}, self.zfs.volumes)
