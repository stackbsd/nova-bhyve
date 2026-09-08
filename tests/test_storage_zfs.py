"""Tests for the ZFS helpers."""

import unittest
from unittest import mock

import nova.conf

from nova_bhyve import exception
from nova_bhyve.storage import zfs

CONF = nova.conf.CONF


class PrefixAssertionTestCase(unittest.TestCase):
    """The guard between a malformed name and a destroy on the wrong dataset."""

    def setUp(self):
        """Configure a known root dataset."""
        super().setUp()
        CONF.set_override("zfs_dataset", "tank/nova", group="bhyve")
        self.addCleanup(CONF.clear_override, "zfs_dataset", group="bhyve")

    def test_the_root_itself_is_allowed(self):
        """The configured root dataset passes the assertion."""
        zfs.assert_under_root("tank/nova")

    def test_a_child_is_allowed(self):
        """A dataset under the root passes the assertion."""
        zfs.assert_under_root("tank/nova/instances/abc")

    def test_a_sibling_is_refused(self):
        """A string-prefix match that is not a path match is refused."""
        self.assertRaises(
            exception.BhyveDriverError, zfs.assert_under_root, "tank/novaXXX"
        )

    def test_an_unrelated_pool_is_refused(self):
        """A dataset in an unrelated tree is refused."""
        self.assertRaises(
            exception.BhyveDriverError,
            zfs.assert_under_root,
            "tank/other/instances/abc",
        )

    def test_a_parent_is_refused(self):
        """The root's own parent is refused."""
        self.assertRaises(exception.BhyveDriverError, zfs.assert_under_root, "tank")

    def test_an_empty_dataset_config_is_refused(self):
        """An unset zfs_dataset refuses every dataset."""
        CONF.set_override("zfs_dataset", None, group="bhyve")
        self.assertRaises(
            exception.BhyveDriverError, zfs.assert_under_root, "tank/nova"
        )

    def test_destroy_refuses_a_dataset_outside_the_root(self):
        """destroy_volume never runs zfs for a dataset outside the root."""
        with mock.patch.object(zfs, "run") as run:
            self.assertRaises(
                exception.BhyveDriverError, zfs.destroy_volume, "tank/somethingelse"
            )
        run.assert_not_called()


class VolsizeTestCase(unittest.TestCase):
    """Volume size alignment."""

    def test_alignment_rounds_up_to_the_next_mib(self):
        """Sizes round up to the next MiB boundary."""
        self.assertEqual(1024 * 1024, zfs.align_volsize(1))
        self.assertEqual(1024 * 1024, zfs.align_volsize(1024 * 1024))
        self.assertEqual(2 * 1024 * 1024, zfs.align_volsize(1024 * 1024 + 1))

    def test_alignment_of_zero_is_zero(self):
        """Zero stays zero."""
        self.assertEqual(0, zfs.align_volsize(0))


class SetVolsizeTestCase(unittest.TestCase):
    """set_volsize grows only; shrinking a live filesystem destroys data."""

    def setUp(self):
        """Configure a known root dataset."""
        super().setUp()
        CONF.set_override("zfs_dataset", "tank/nova", group="bhyve")
        self.addCleanup(CONF.clear_override, "zfs_dataset", group="bhyve")

    def test_shrinking_is_refused(self):
        """A target smaller than the current volsize raises before any zfs run."""
        with (
            mock.patch.object(zfs, "volsize_bytes", return_value=10 << 20),
            mock.patch.object(zfs, "run") as run,
        ):
            self.assertRaises(
                exception.BhyveDriverError,
                zfs.set_volsize,
                "tank/nova/instances/a",
                5 << 20,
            )
        run.assert_not_called()

    def test_growing_to_the_same_size_is_a_no_op(self):
        """An equal target issues no zfs command."""
        with (
            mock.patch.object(zfs, "volsize_bytes", return_value=10 << 20),
            mock.patch.object(zfs, "run") as run,
        ):
            zfs.set_volsize("tank/nova/instances/a", 10 << 20)
        run.assert_not_called()

    def test_growing_issues_the_set(self):
        """A larger target issues zfs set."""
        with (
            mock.patch.object(zfs, "volsize_bytes", return_value=10 << 20),
            mock.patch.object(zfs, "run") as run,
        ):
            zfs.set_volsize("tank/nova/instances/a", 20 << 20)
        self.assertEqual("set", run.call_args[0][0])


class ClonesOfTestCase(unittest.TestCase):
    """Parsing zfs get clones, which guards teardown against leaking a zvol."""

    def test_no_clones_reads_as_empty(self):
        """A dash or empty value means no clones."""
        for value in ("-\n", "\n", ""):
            with mock.patch.object(zfs, "run", return_value=value):
                self.assertEqual([], zfs.clones_of("tank/nova/a@s"))

    def test_multiple_clones_are_split_on_commas(self):
        """A comma-separated value becomes a list of dataset names."""
        with mock.patch.object(zfs, "run", return_value="tank/nova/b,tank/nova/c\n"):
            self.assertEqual(
                ["tank/nova/b", "tank/nova/c"], zfs.clones_of("tank/nova/a@s")
            )
