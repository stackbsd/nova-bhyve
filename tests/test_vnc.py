"""Tests for the VNC console path."""

import os
import tempfile
import unittest
from unittest import mock

import nova.conf

from nova import exception as nova_exception
from nova_bhyve import driver as driver_mod
from nova_bhyve import machine
from nova_bhyve.machine import direct

CONF = nova.conf.CONF

UUID = "11111111-2222-3333-4444-555555555555"
OTHER = "99999999-8888-7777-6666-555555555555"


def make_spec(**kwargs):
    """Return a MachineSpec with test defaults, overridable per test."""
    base = dict(
        uuid=UUID,
        title="test",
        vcpus=1,
        mem_mib=1024,
        root_dev="/dev/zvol/tank/nova/instances/inst",
    )
    base.update(kwargs)
    return machine.MachineSpec(**base)


class FbufArgvTestCase(unittest.TestCase):
    """The framebuffer and tablet on the bhyve command line."""

    def setUp(self):
        """Configure the VNC listen address and a DirectMachine under test."""
        super().setUp()
        CONF.set_override("server_listen", "127.0.0.1", group="vnc")
        self.addCleanup(CONF.clear_override, "server_listen", group="vnc")
        self.machine = direct.DirectMachine()

    def _slots(self, argv):
        """Return the -s arguments in order."""
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-s"]

    def test_no_framebuffer_without_a_port(self):
        """A spec without a port gets neither fbuf nor tablet."""
        slots = self._slots(self.machine._build_argv(make_spec()))
        self.assertFalse(any("fbuf" in s or "xhci" in s for s in slots))

    def test_framebuffer_and_tablet_with_a_port(self):
        """A spec with a port gets the fbuf and the tablet at fixed slots."""
        slots = self._slots(self.machine._build_argv(make_spec(vnc_port=5903)))
        self.assertIn("29:0,fbuf,tcp=127.0.0.1:5903", slots)
        self.assertIn("30:0,xhci,tablet", slots)


class PortAllocationTestCase(unittest.TestCase):
    """Port allocation is stable per instance and collision-free."""

    def setUp(self):
        """Configure a temporary instances path and a DirectMachine."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.machine = direct.DirectMachine()
        mock.patch.object(direct.shutil, "copyfile").start()
        self.addCleanup(mock.patch.stopall)

    def test_define_allocates_the_lowest_free_port(self):
        """A taken port pushes the allocation to the next free one."""
        self.machine._write_spec(make_spec(uuid=OTHER, vnc_port=5900))
        self.machine.define(make_spec(vnc_port=machine.VNC_PORT_AUTO))
        self.assertEqual(5901, self.machine._read_spec(UUID).vnc_port)

    def test_a_freed_port_is_reused(self):
        """A gap in the allocated ports is filled first."""
        self.machine._write_spec(make_spec(uuid=OTHER, vnc_port=5901))
        self.machine.define(make_spec(vnc_port=machine.VNC_PORT_AUTO))
        self.assertEqual(5900, self.machine._read_spec(UUID).vnc_port)

    def test_redefine_keeps_the_instance_port(self):
        """A redefine keeps the port a console URL already points at."""
        self.machine._write_spec(make_spec(vnc_port=5907))
        self.machine.define(make_spec(vnc_port=machine.VNC_PORT_AUTO))
        self.assertEqual(5907, self.machine._read_spec(UUID).vnc_port)

    def test_vnc_disabled_stays_portless(self):
        """A spec that asks for no framebuffer gets no port."""
        self.machine.define(make_spec())
        self.assertIsNone(self.machine._read_spec(UUID).vnc_port)

    def test_spec_written_before_this_field_existed_reads_as_portless(self):
        """A spec file predating the vnc_port field reads as portless."""
        spec_dir = os.path.join(self.tmp.name, UUID)
        os.makedirs(spec_dir)
        with open(os.path.join(spec_dir, direct.SPEC_FILE), "w") as handle:
            handle.write(
                '{"uuid": "%s", "title": "t", "vcpus": 1, '
                '"mem_mib": 512, "root_dev": "/dev/zvol/x"}' % UUID
            )
        self.assertIsNone(self.machine._read_spec(UUID).vnc_port)


class GetVncConsoleTestCase(unittest.TestCase):
    """get_vnc_console answers from the persisted spec."""

    def setUp(self):
        """Build a driver and configure the proxy client address."""
        super().setUp()
        self.driver = driver_mod.BhyveDriver(mock.Mock())
        self.instance = mock.Mock(uuid=UUID)
        CONF.set_override("server_proxyclient_address", "127.0.0.1", group="vnc")
        self.addCleanup(CONF.clear_override, "server_proxyclient_address", group="vnc")

    def test_answers_from_the_spec(self):
        """The console address is the configured host and the spec's port."""
        with mock.patch.object(
            self.driver._machine, "describe", return_value={"vnc_port": 5902}
        ):
            console = self.driver.get_vnc_console(mock.Mock(), self.instance)
        self.assertEqual("127.0.0.1", console.host)
        self.assertEqual(5902, console.port)

    def test_no_port_is_unavailable_not_a_guess(self):
        """An instance without a port raises ConsoleTypeUnavailable."""
        with mock.patch.object(self.driver._machine, "describe", return_value={}):
            self.assertRaises(
                nova_exception.ConsoleTypeUnavailable,
                self.driver.get_vnc_console,
                mock.Mock(),
                self.instance,
            )
