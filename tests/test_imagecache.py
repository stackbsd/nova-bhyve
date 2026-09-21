"""Tests for removal of unused cached images."""

import unittest
from unittest import mock

import nova.conf

from nova_bhyve import driver as driver_mod
from nova_bhyve import exception, imagecache, images
from nova_bhyve.storage import zfs

CONF = nova.conf.CONF

IMAGE_A = "aaaaaaaa-0000-0000-0000-000000000001"
IMAGE_B = "bbbbbbbb-0000-0000-0000-000000000002"
MIN_AGE = 3600


def make_instance(image_ref):
    """Return an instance carrying only what the cache manager reads."""
    return mock.Mock(image_ref=image_ref)


class ImageCacheManagerTestCase(unittest.TestCase):
    """update removes images only once unused for the minimum age."""

    def setUp(self):
        """Build a manager over a mocked cache holding two images."""
        super().setUp()
        CONF.set_override(
            "remove_unused_original_minimum_age_seconds", MIN_AGE, group="image_cache"
        )
        self.addCleanup(
            CONF.clear_override,
            "remove_unused_original_minimum_age_seconds",
            group="image_cache",
        )
        self.cloned = set()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(
            images, "get_cached_images", return_value=[IMAGE_A, IMAGE_B]
        ).start()
        mock.patch.object(
            images, "is_inuse", side_effect=lambda image_id: image_id in self.cloned
        ).start()
        self.remove = mock.patch.object(
            images, "remove_image", return_value=True
        ).start()
        self.clock = mock.patch.object(imagecache.time, "time").start()
        self.clock.return_value = 1000.0
        self.manager = imagecache.ImageCacheManager()

    def _pass(self, *image_refs, after=0):
        """Run one cache manager pass some seconds after the last."""
        self.clock.return_value += after
        self.manager.update(None, [make_instance(ref) for ref in image_refs])

    def test_a_newly_unused_image_is_kept(self):
        """An image first seen unused is younger than the minimum age."""
        self._pass()
        self.remove.assert_not_called()

    def test_an_image_unused_for_the_minimum_age_is_removed(self):
        """An image unused across the minimum age is removed."""
        self._pass()
        self._pass(after=MIN_AGE)
        self.assertEqual(
            [mock.call(IMAGE_A), mock.call(IMAGE_B)], self.remove.call_args_list
        )
        self.assertEqual({}, self.manager.unused_since)

    def test_a_zero_minimum_age_removes_on_the_first_pass(self):
        """With no minimum age an unused image goes at once."""
        CONF.set_override(
            "remove_unused_original_minimum_age_seconds", 0, group="image_cache"
        )
        self._pass(IMAGE_B)
        self.remove.assert_called_once_with(IMAGE_A)

    def test_a_referenced_image_is_kept(self):
        """An image some instance names as its image_ref is kept."""
        self._pass(IMAGE_A)
        self._pass(IMAGE_A, after=MIN_AGE)
        self.remove.assert_called_once_with(IMAGE_B)

    def test_a_cloned_image_is_kept_without_any_instance(self):
        """An image with clones is kept whatever nova's instance list says."""
        self.cloned.add(IMAGE_A)
        self._pass()
        self._pass(after=MIN_AGE)
        self.remove.assert_called_once_with(IMAGE_B)

    def test_use_restarts_the_clock(self):
        """An image used again must age from scratch afterwards."""
        self._pass(IMAGE_B)
        self._pass(IMAGE_A, IMAGE_B, after=MIN_AGE - 1)
        self._pass(IMAGE_B, after=1)
        self.remove.assert_not_called()
        self._pass(IMAGE_B, after=MIN_AGE)
        self.remove.assert_called_once_with(IMAGE_A)

    def test_an_image_cloned_at_the_last_moment_is_kept(self):
        """A removal that finds new clones leaves the image alone."""
        self.remove.return_value = False
        self._pass()
        self._pass(after=MIN_AGE)
        self.assertIn(IMAGE_A, self.manager.unused_since)

    def test_a_failed_removal_does_not_stop_the_pass(self):
        """One image failing to remove leaves the rest to be removed."""
        self.remove.side_effect = [exception.BhyveDriverError(reason="busy"), True]
        self._pass()
        self._pass(after=MIN_AGE)
        self.assertEqual(2, self.remove.call_count)
        self.assertEqual([IMAGE_A], list(self.manager.unused_since))

    def test_removal_can_be_switched_off(self):
        """remove_unused_base_images=False leaves the cache untouched."""
        self.manager.remove_unused_base_images = False
        self._pass()
        self._pass(after=MIN_AGE)
        self.remove.assert_not_called()


class CacheHelpersTestCase(unittest.TestCase):
    """The cache is listed, probed and removed through zfs."""

    def setUp(self):
        """Point the driver at a test dataset and mock the zfs layer."""
        super().setUp()
        CONF.set_override("zfs_dataset", "tank/nova", group="bhyve")
        self.addCleanup(CONF.clear_override, "zfs_dataset", group="bhyve")
        self.addCleanup(mock.patch.stopall)
        self.is_cached = mock.patch.object(
            images, "is_cached", return_value=True
        ).start()
        self.clones_of = mock.patch.object(zfs, "clones_of", return_value=[]).start()
        self.destroy = mock.patch.object(images, "destroy_cache").start()
        mock.patch.object(images.lockutils, "lock").start()

    def test_image_ids_are_the_zvol_basenames(self):
        """Each zvol under images/ is one cached image."""
        with mock.patch.object(
            zfs, "run", return_value="tank/nova/images/%s\n" % IMAGE_A
        ) as run:
            self.assertEqual([IMAGE_A], images.get_cached_images())
        self.assertEqual("tank/nova/images", run.call_args.args[-1])

    def test_clones_are_read_from_the_base_snapshot(self):
        """is_inuse asks zfs about the image's @base snapshot."""
        self.clones_of.return_value = ["tank/nova/instances/inst"]
        self.assertTrue(images.is_inuse(IMAGE_A))
        self.clones_of.assert_called_once_with("tank/nova/images/%s@base" % IMAGE_A)

    def test_a_partial_entry_has_no_clones(self):
        """A cache zvol without @base is never asked for its clones."""
        self.is_cached.return_value = False
        self.assertFalse(images.is_inuse(IMAGE_A))
        self.clones_of.assert_not_called()

    def test_remove_holds_the_image_lock(self):
        """Removal takes the lock that populating the cache takes."""
        self.assertTrue(images.remove_image(IMAGE_A))
        images.lockutils.lock.assert_called_once_with(
            "nova-bhyve-image-" + IMAGE_A, external=True
        )
        self.destroy.assert_called_once_with(IMAGE_A)

    def test_remove_leaves_a_cloned_image(self):
        """An image that gained a clone is not destroyed."""
        self.clones_of.return_value = ["tank/nova/instances/inst"]
        self.assertFalse(images.remove_image(IMAGE_A))
        self.destroy.assert_not_called()


class DriverHookTestCase(unittest.TestCase):
    """The compute manager's periodic task reaches the cache manager."""

    def test_capability_is_advertised(self):
        """Nova only runs the cache pass for drivers with an image cache."""
        self.assertTrue(driver_mod.BhyveDriver.capabilities["has_imagecache"])

    def test_manage_image_cache_runs_an_update(self):
        """manage_image_cache hands the instance list to the manager."""
        with mock.patch.object(driver_mod.machine_pkg, "get_machine"):
            driver = driver_mod.BhyveDriver(mock.Mock())
        driver._image_cache_manager = mock.Mock()
        driver.manage_image_cache("ctx", ["inst"])
        driver._image_cache_manager.update.assert_called_once_with("ctx", ["inst"])


if __name__ == "__main__":
    unittest.main()
