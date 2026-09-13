"""Tests for the direct-bhyve mechanism's command line and state machine."""

import os
import tempfile
import unittest
from unittest import mock

import nova.conf
from nova.compute import power_state

from nova_bhyve import machine
from nova_bhyve.machine import direct

CONF = nova.conf.CONF


def make_spec(**kwargs):
    """Return a MachineSpec with test defaults, overridable per test."""
    base = dict(
        uuid="11111111-2222-3333-4444-555555555555",
        title="test",
        vcpus=2,
        mem_mib=1024,
        root_dev="/dev/zvol/tank/nova/instances/inst",
        iso_path=None,
        volumes=(),
    )
    base.update(kwargs)
    return machine.MachineSpec(**base)


class BuildArgvTestCase(unittest.TestCase):
    """The bhyve command line, slot order above all."""

    def setUp(self):
        """Configure paths and a DirectMachine under test."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        CONF.set_override("bootrom_path", "/fw/CODE.fd", group="bhyve")
        self.addCleanup(CONF.clear_override, "bootrom_path", group="bhyve")
        CONF.set_override("ignore_unimplemented_msr", False, group="bhyve")
        self.addCleanup(CONF.clear_override, "ignore_unimplemented_msr", group="bhyve")
        self.machine = direct.DirectMachine()

    def _slots(self, argv):
        """Return the -s arguments in order."""
        return [argv[i + 1] for i, a in enumerate(argv) if a == "-s"]

    def test_minimal_domain(self):
        """A minimal spec yields hostbridge, lpc and the root disk."""
        argv = self.machine._build_argv(make_spec())
        self.assertEqual("/usr/sbin/bhyve", argv[0])
        self.assertEqual(
            [
                "0:0,hostbridge",
                "1:0,lpc",
                "2:0,virtio-blk,/dev/zvol/tank/nova/instances/inst",
            ],
            self._slots(argv),
        )
        self.assertEqual("11111111-2222-3333-4444-555555555555", argv[-1])

    def test_cdrom_precedes_the_root_disk(self):
        """The cdrom slot comes before the root disk."""
        # Both mechanisms emit the same slot order, so an instance defined
        # under one presents its disks to the guest in the same order under
        # the other.
        argv = self.machine._build_argv(make_spec(iso_path="/i/config.iso"))
        self.assertEqual(
            [
                "0:0,hostbridge",
                "1:0,lpc",
                "2:0,ahci,cd:/i/config.iso",
                "3:0,virtio-blk,/dev/zvol/tank/nova/instances/inst",
            ],
            self._slots(argv),
        )

    def test_root_disk_is_always_the_first_virtio_blk(self):
        """The root disk precedes attached volumes in discovery order."""
        # The guest names virtio disks by discovery order, so the root disk
        # must come first or the guest boots the wrong device.
        argv = self.machine._build_argv(
            make_spec(
                iso_path="/i/config.iso",
                volumes=(
                    machine.DiskSpec(device_path="/dev/zvol/tank/v1", target_dev="vdb"),
                    machine.DiskSpec(device_path="/dev/zvol/tank/v2", target_dev="vdc"),
                ),
            )
        )
        blk = [s for s in self._slots(argv) if "virtio-blk" in s]
        self.assertEqual(3, len(blk))
        self.assertIn("instances/inst", blk[0])
        self.assertIn("/dev/zvol/tank/v1", blk[1])
        self.assertIn("/dev/zvol/tank/v2", blk[2])

    def test_console_goes_to_stdout(self):
        """The console is bhyve's stdout, which daemon(8) appends to a file."""
        argv = self.machine._build_argv(make_spec())
        self.assertIn("com1,stdio", argv)

    def test_nvram_is_per_instance(self):
        """The bootrom argument points at the instance's own var store."""
        argv = self.machine._build_argv(make_spec())
        bootrom = argv[argv.index("-l") + 1]
        self.assertTrue(bootrom.startswith("bootrom,/fw/CODE.fd,"))
        self.assertIn("11111111-2222-3333-4444-555555555555", bootrom)
        self.assertTrue(bootrom.endswith("nvram.fd"))

    def test_msr_workaround_is_off_by_default(self):
        """-w is absent unless configured."""
        self.assertNotIn("-w", self.machine._build_argv(make_spec()))

    def test_msr_workaround_is_added_when_configured(self):
        """ignore_unimplemented_msr adds -w."""
        CONF.set_override("ignore_unimplemented_msr", True, group="bhyve")
        self.assertIn("-w", self.machine._build_argv(make_spec()))


class StateTestCase(unittest.TestCase):
    """state() tells a clean poweroff from a crash from plain files."""

    def setUp(self):
        """Create an instance directory and a DirectMachine under test."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.uuid = "11111111-2222-3333-4444-555555555555"
        self.dir = os.path.join(self.tmp.name, self.uuid)
        os.makedirs(self.dir)
        self.machine = direct.DirectMachine()

    def _define(self):
        """Persist a spec for the test instance."""
        with mock.patch.object(direct.shutil, "copyfile"):
            self.machine._write_spec(make_spec(uuid=self.uuid))

    def _write(self, name, text):
        """Write a state file into the instance directory."""
        with open(os.path.join(self.dir, name), "w") as handle:
            handle.write(text)

    def test_undefined_instance_is_nostate(self):
        """No spec file reads as NOSTATE."""
        state, reason = self.machine.state(self.uuid)
        self.assertEqual(power_state.NOSTATE, state)
        self.assertEqual("undefined", reason)

    def test_live_pid_is_running(self):
        """A pidfile naming a live process reads as RUNNING."""
        self._define()
        self._write("bhyve.pid", "%d\n" % os.getpid())
        self.assertEqual(power_state.RUNNING, self.machine.state(self.uuid)[0])

    def test_clean_poweroff_is_shutdown(self):
        """A poweroff verdict reads as SHUTDOWN."""
        self._define()
        self._write("exit.status", "poweroff\n")
        state, reason = self.machine.state(self.uuid)
        self.assertEqual(power_state.SHUTDOWN, state)
        self.assertIn("poweroff", reason)

    def test_halted_is_shutdown(self):
        """A halted verdict reads as SHUTDOWN."""
        self._define()
        self._write("exit.status", "halted\n")
        self.assertEqual(power_state.SHUTDOWN, self.machine.state(self.uuid)[0])

    def test_signal_death_is_crashed(self):
        """A signal verdict reads as CRASHED."""
        self._define()
        self._write("exit.status", "signal 9\n")
        state, reason = self.machine.state(self.uuid)
        self.assertEqual(power_state.CRASHED, state)
        self.assertIn("signal 9", reason)

    def test_triple_fault_is_crashed(self):
        """A triple-fault verdict reads as CRASHED."""
        self._define()
        self._write("exit.status", "triple-fault\n")
        self.assertEqual(power_state.CRASHED, self.machine.state(self.uuid)[0])

    def test_unknown_verdict_is_crashed_not_shutdown(self):
        """An unrecognised verdict reads as CRASHED, the honest direction."""
        self._define()
        self._write("exit.status", "exit 42\n")
        self.assertEqual(power_state.CRASHED, self.machine.state(self.uuid)[0])

    def test_orphaned_vmm_object_is_crashed(self):
        """A vmm object with no verdict means the supervisor died untidily."""
        self._define()
        with mock.patch.object(
            direct.DirectMachine, "_vmm_present", staticmethod(lambda _u: True)
        ):
            state, reason = self.machine.state(self.uuid)
        self.assertEqual(power_state.CRASHED, state)
        self.assertIn("vmm object", reason)

    def test_no_pid_no_verdict_no_object_is_shutdown(self):
        """Nothing at all reads as SHUTDOWN."""
        self._define()
        self.assertEqual(power_state.SHUTDOWN, self.machine.state(self.uuid)[0])

    def test_dead_pid_falls_through_to_the_verdict(self):
        """A stale pidfile does not read as RUNNING."""
        self._define()
        self._write("bhyve.pid", "999999\n")
        self._write("exit.status", "poweroff\n")
        self.assertEqual(power_state.SHUTDOWN, self.machine.state(self.uuid)[0])


class SpecRoundTripTestCase(unittest.TestCase):
    """Spec persistence, the on-disk replacement for a daemon's memory."""

    def setUp(self):
        """Configure a temporary instances path."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.machine = direct.DirectMachine()

    def test_spec_survives_a_write_and_read(self):
        """A written spec reads back equal, volumes included."""
        spec = make_spec(
            iso_path="/i/config.iso",
            volumes=(
                machine.DiskSpec(device_path="/dev/zvol/tank/v1", target_dev="vdb"),
            ),
        )
        self.machine._write_spec(spec)
        self.assertEqual(spec, self.machine._read_spec(spec.uuid))

    def test_missing_spec_reads_as_none(self):
        """An absent spec reads as None."""
        self.assertIsNone(self.machine._read_spec("no-such-instance"))

    def test_list_uuids_reports_defined_instances_only(self):
        """A directory with no spec file is not an instance."""
        # For example one holding only a config drive from a half-finished
        # spawn.
        self.machine._write_spec(make_spec(uuid="aaaa"))
        os.makedirs(os.path.join(self.tmp.name, "bbbb"))
        self.assertEqual(["aaaa"], self.machine.list_uuids())


class StartTestCase(unittest.TestCase):
    """start() does not return until the guest is really running."""

    def setUp(self):
        """Create a defined instance and stub out the privileged calls."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.uuid = "11111111-2222-3333-4444-555555555555"
        self.dir = os.path.join(self.tmp.name, self.uuid)
        os.makedirs(self.dir)
        self.machine = direct.DirectMachine()
        with mock.patch.object(direct.shutil, "copyfile"):
            self.machine._write_spec(make_spec(uuid=self.uuid))
        self.start_domain = mock.patch.object(direct.privsep, "start_domain").start()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.machine, "_await_nics").start()
        mock.patch.object(direct.time, "sleep").start()

    def _write(self, name, text):
        """Write a state file into the instance directory."""
        with open(os.path.join(self.dir, name), "w") as handle:
            handle.write(text)

    def test_start_waits_for_a_live_pid(self):
        """start() returns once the pidfile names a live process."""

        # the supervisor writes the pidfile some time after start_domain
        # returns, so it only appears once start() has slept on a poll
        def supervisor_catches_up(_seconds):
            self._write("bhyve.pid", "%d\n" % os.getpid())

        direct.time.sleep.side_effect = supervisor_catches_up
        self.machine.start(self.uuid)
        self.start_domain.assert_called_once()
        direct.time.sleep.assert_called_once()

    def test_start_raises_when_the_guest_exits(self):
        """A verdict appearing during the wait means the boot failed."""

        def exited(*_args, **_kwargs):
            self._write("exit.status", "exit 4\n")

        self.start_domain.side_effect = exited
        with self.assertRaises(direct.exception.MachineError) as ctx:
            self.machine.start(self.uuid)
        self.assertIn("exited while starting", str(ctx.exception))

    def test_start_raises_on_timeout(self):
        """No pid and no verdict before the deadline raises."""
        clock = iter([0, 0, direct.START_WAIT_SECONDS + 1])
        with mock.patch.object(direct.time, "time", lambda: next(clock)):
            with self.assertRaises(direct.exception.MachineError) as ctx:
                self.machine.start(self.uuid)
        self.assertIn("did not start", str(ctx.exception))

    def test_start_clears_the_previous_verdict(self):
        """A stale verdict from the last run must not fail this start."""
        self._write("exit.status", "poweroff\n")
        self._write("bhyve.pid", "%d\n" % os.getpid())
        # the pidfile above is "live" so state() reads RUNNING on the first
        # poll, but start() must first unlink the old verdict or the early
        # RUNNING check would have returned before launching. remove it
        # for that check only.
        os.unlink(os.path.join(self.dir, "bhyve.pid"))

        def launched(*_args, **_kwargs):
            self._write("bhyve.pid", "%d\n" % os.getpid())

        self.start_domain.side_effect = launched
        self.machine.start(self.uuid)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "exit.status")))
