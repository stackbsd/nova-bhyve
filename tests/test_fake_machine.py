"""Tests for the fake mechanism."""

import os
import tempfile
import unittest
from unittest import mock

import nova.conf
from nova.compute import power_state

from nova_bhyve import exception, machine
from nova_bhyve.machine import direct, fake

CONF = nova.conf.CONF

UUID = "11111111-2222-3333-4444-555555555555"


class FakeMachineTestCase(unittest.TestCase):
    """The fake mechanism keeps state on disk and never calls privsep."""

    def setUp(self):
        """Point instances_path at a scratch dir and forbid privsep."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.root = os.path.join(self.tmp.name, "root")
        self.volume = os.path.join(self.tmp.name, "volume")
        for path in (self.root, self.volume):
            open(path, "w").close()
        self.privsep = mock.patch.object(direct, "privsep").start()
        mock.patch.object(direct.shutil, "copyfile").start()
        mock.patch.object(
            direct.DirectMachine, "_nic_present", return_value=True
        ).start()
        self.addCleanup(mock.patch.stopall)
        self.machine = fake.FakeMachine()

    def define(self, **kwargs):
        """Define the test instance."""
        base = dict(
            uuid=UUID,
            title="test",
            vcpus=1,
            mem_mib=512,
            root_dev=self.root,
            volumes=(machine.DiskSpec(self.volume, "vdb"),),
            nics=(machine.NicSpec("tap0", "br0", "fa:16:3e:00:00:01"),),
        )
        base.update(kwargs)
        self.machine.define(machine.MachineSpec(**base))

    def test_connect_skips_the_privsep_probe(self):
        """connect() succeeds without a privileged helper."""
        self.machine.connect()
        self.privsep.vm_stats.assert_not_called()

    def test_undefined_instance_is_nostate(self):
        """An instance with no spec reads as NOSTATE."""
        self.assertEqual(power_state.NOSTATE, self.machine.state(UUID)[0])

    def test_defined_instance_is_shutdown(self):
        """A defined instance that was never started reads as SHUTDOWN."""
        self.define()
        self.assertEqual(power_state.SHUTDOWN, self.machine.state(UUID)[0])
        self.assertEqual([UUID], self.machine.list_uuids())

    def test_start_marks_running(self):
        """start() leaves the instance RUNNING without launching bhyve."""
        self.define()
        self.machine.start(UUID)
        self.assertEqual(power_state.RUNNING, self.machine.state(UUID)[0])
        self.privsep.start_domain.assert_not_called()

    def test_start_logs_the_argv_to_the_console(self):
        """The bhyve command line is readable as console output."""
        self.define()
        self.machine.start(UUID)
        with open(self.machine.describe(UUID)["console_log"]) as handle:
            text = handle.read()
        self.assertIn("virtio-blk,%s" % self.volume, text)
        self.assertIn("virtio-net,tap0", text)

    def test_start_refuses_missing_devices(self):
        """A disk path that does not exist fails the start."""
        self.define()
        os.unlink(self.volume)
        with self.assertRaises(exception.MachineError) as ctx:
            self.machine.start(UUID)
        self.assertIn(self.volume, str(ctx.exception))
        self.assertEqual(power_state.SHUTDOWN, self.machine.state(UUID)[0])

    def test_start_waits_for_taps(self):
        """start() still requires the l2 agent to create the taps."""
        self.define()
        with mock.patch.object(self.machine, "_await_nics") as await_nics:
            self.machine.start(UUID)
        await_nics.assert_called_once()

    def test_shutdown_and_destroy_mark_stopped(self):
        """Both stop paths return the instance to SHUTDOWN."""
        self.define()
        for stop in (
            lambda: self.machine.shutdown(UUID, 30),
            lambda: self.machine.destroy(UUID),
        ):
            self.machine.start(UUID)
            stop()
            self.assertEqual(power_state.SHUTDOWN, self.machine.state(UUID)[0])

    def test_marker_from_before_the_boot_is_not_running(self):
        """An instance marked running before a host reboot is shut down."""
        self.define()
        self.machine.start(UUID)
        later = os.path.getmtime(fake.running_path(UUID)) + 60
        with mock.patch.object(fake.psutil, "boot_time", return_value=later):
            self.assertEqual(power_state.SHUTDOWN, self.machine.state(UUID)[0])

    def test_running_instance_refuses_cold_attach(self):
        """The stopped-only attach rule is inherited."""
        self.define()
        self.machine.start(UUID)
        with self.assertRaises(exception.MachineError):
            self.machine.attach_disk(UUID, machine.DiskSpec(self.volume, "vdc"))

    def test_running_instance_refuses_redefine(self):
        """define() refuses to replace a running instance."""
        self.define()
        self.machine.start(UUID)
        with self.assertRaises(exception.MachineError):
            self.define()

    def test_undefine_removes_the_instance(self):
        """undefine() forgets the instance without reaping a vmm."""
        self.define()
        self.machine.start(UUID)
        self.machine.undefine(UUID)
        self.assertEqual(power_state.NOSTATE, self.machine.state(UUID)[0])
        self.assertEqual([], self.machine.list_uuids())
        self.privsep.reap_vm.assert_not_called()

    def test_describe_has_no_stats(self):
        """describe() never asks bhyvectl for statistics."""
        self.define(vnc_port=5901)
        details = self.machine.describe(UUID)
        self.assertEqual(5901, details["vnc_port"])
        self.assertNotIn("stats", details)
        self.privsep.vm_stats.assert_not_called()
