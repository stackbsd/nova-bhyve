"""Tests for mechanism selection."""

import unittest

import nova.conf

from nova_bhyve import machine

CONF = nova.conf.CONF


class GetMachineTestCase(unittest.TestCase):
    """get_machine honours [bhyve] mechanism."""

    def _override(self, value):
        """Override the mechanism option for one test."""
        CONF.set_override("mechanism", value, group="bhyve")
        self.addCleanup(CONF.clear_override, "mechanism", group="bhyve")

    def test_direct_is_the_default(self):
        """With no override, the direct mechanism is selected."""
        self.assertEqual("direct", CONF.bhyve.mechanism)
        self.assertEqual("DirectMachine", type(machine.get_machine()).__name__)

    def test_direct_is_selected_explicitly(self):
        """mechanism=direct selects DirectMachine."""
        self._override("direct")
        self.assertEqual("DirectMachine", type(machine.get_machine()).__name__)
