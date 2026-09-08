"""Tests for the argument validation in the privileged entrypoints."""

import unittest

from nova.virt.bhyve import privsep


class NameValidationTestCase(unittest.TestCase):
    """check_name accepts only instance-uuid-shaped names."""

    GOOD = "11111111-2222-3333-4444-555555555555"

    def test_an_instance_uuid_is_accepted(self):
        """A well-formed instance uuid passes and is returned."""
        self.assertEqual(self.GOOD, privsep.check_name(self.GOOD))

    def test_path_traversal_is_refused(self):
        """Path-shaped and empty names are refused."""
        for bad in ("../../etc/passwd", "/dev/vmm/x", "a/b", "", ".", "..", "x" * 200):
            self.assertRaises(ValueError, privsep.check_name, bad)

    def test_shell_metacharacters_are_refused(self):
        """Names carrying shell metacharacters are refused."""
        for bad in ("a;rm -rf /", "$(id)", "`id`", "a b"):
            self.assertRaises(ValueError, privsep.check_name, bad)


class StateDirValidationTestCase(unittest.TestCase):
    """check_state_dir ties a state directory to its instance."""

    GOOD = "11111111-2222-3333-4444-555555555555"

    def test_a_matching_directory_is_accepted(self):
        """An absolute directory named after the instance passes."""
        path = "/var/db/nova/instances/" + self.GOOD
        self.assertEqual(path, privsep.check_state_dir(path, self.GOOD))

    def test_a_trailing_slash_is_tolerated(self):
        """A trailing slash does not defeat the basename comparison."""
        path = "/var/db/nova/instances/" + self.GOOD + "/"
        privsep.check_state_dir(path, self.GOOD)

    def test_a_relative_directory_is_refused(self):
        """A relative path is refused."""
        self.assertRaises(
            ValueError, privsep.check_state_dir, "instances/" + self.GOOD, self.GOOD
        )

    def test_a_directory_for_another_instance_is_refused(self):
        """One instance's teardown cannot touch another's pidfiles."""
        other = "99999999-8888-7777-6666-555555555555"
        self.assertRaises(
            ValueError,
            privsep.check_state_dir,
            "/var/db/nova/instances/" + other,
            self.GOOD,
        )

    def test_a_bad_name_is_refused_even_with_a_matching_directory(self):
        """A malformed name fails regardless of the directory shape."""
        self.assertRaises(ValueError, privsep.check_state_dir, "/tmp/../etc", "../etc")
