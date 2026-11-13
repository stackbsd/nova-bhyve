"""Tests for the bhyve supervisor, driven by a fake child process."""

# The guest-initiated reboot path cannot be exercised on a real host: with
# -l com1,stdio the console is write-only, so there is no way to log into a
# guest and type reboot. The reboot logic is verified here or not at all.

import contextlib
import io
import os
import signal
import tempfile
import unittest
from unittest import mock

from nova_bhyve import supervisor


class FakeChild:
    """Stands in for subprocess.Popen with a scripted exit status."""

    def __init__(self, status):
        """Record the exit status this child will report."""
        self.pid = 4242
        self._status = status
        self.signals = []

    def wait(self):
        """Return the scripted exit status."""
        return self._status

    def poll(self):
        """Report the child as still running."""
        return None

    def send_signal(self, signum):
        """Record a signal instead of delivering one."""
        self.signals.append(signum)


class SupervisorTestCase(unittest.TestCase):
    """The supervisor's respawn loop and exit recording."""

    def setUp(self):
        """Create a temporary state directory."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = self.tmp.name
        self.name = "11111111-2222-3333-4444-555555555555"
        # keeps the lines under test off the test runner's stderr
        quiet = supervisor.logging.NullHandler()
        supervisor.LOG.addHandler(quiet)
        self.addCleanup(supervisor.LOG.removeHandler, quiet)

    def _run(self, exit_statuses, stop_after=None):
        """Run the supervisor against a scripted list of child exit codes."""
        sup = supervisor.Supervisor(self.state_dir, self.name, ["/bin/true"])
        spawned = []

        def fake_popen(argv):
            """Hand out the next scripted child."""
            child = FakeChild(exit_statuses[len(spawned)])
            spawned.append(child)
            if stop_after is not None and len(spawned) == stop_after:
                sup.stopping = True
            return child

        with (
            mock.patch.object(supervisor.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(sup, "_reap") as reap,
        ):
            sup.run()
        return sup, spawned, reap

    def _status(self):
        """Read back the recorded exit verdict."""
        with open(os.path.join(self.state_dir, "exit.status")) as handle:
            return handle.read().strip()

    # Reboot.

    def test_reboot_respawns_then_stops_on_poweroff(self):
        """Exit 0 means the guest rebooted and bhyve is re-executed."""
        _sup, spawned, _reap = self._run(
            [supervisor.EXIT_REBOOTED, supervisor.EXIT_POWEROFF]
        )
        self.assertEqual(
            2,
            len(spawned),
            "exit 0 means the guest rebooted and bhyve must be re-executed",
        )
        self.assertEqual("poweroff", self._status())

    def test_repeated_reboots_keep_respawning(self):
        """Every reboot exit respawns until a terminal exit arrives."""
        _sup, spawned, _reap = self._run(
            [supervisor.EXIT_REBOOTED] * 3 + [supervisor.EXIT_POWEROFF]
        )
        self.assertEqual(4, len(spawned))

    def test_reboot_reaps_the_vmm_object_between_runs(self):
        """The leftover vmm object is cleared before the respawn."""
        # It would otherwise collide with the next run's "VM name already
        # exists".
        _sup, _spawned, reap = self._run(
            [supervisor.EXIT_REBOOTED, supervisor.EXIT_POWEROFF]
        )
        self.assertEqual(2, reap.call_count)

    def test_each_reset_is_logged_with_its_restart_count(self):
        """Every reboot exit leaves a line carrying the exit code and count."""
        with self.assertLogs(supervisor.LOG, "WARNING") as logs:
            self._run([supervisor.EXIT_REBOOTED] * 2 + [supervisor.EXIT_POWEROFF])
        self.assertEqual(
            [
                "guest reset (bhyve exit 0), restart 1",
                "guest reset (bhyve exit 0), restart 2",
            ],
            [record.getMessage() for record in logs.records],
        )

    def test_a_log_line_starts_on_its_own_line_with_a_timestamp(self):
        """The format cannot append to a partial line of guest output."""
        record = supervisor.LOG.makeRecord(
            supervisor.LOG.name, 30, __file__, 0, "guest reset", (), None
        )
        line = supervisor.logging.Formatter(supervisor.LOG_FORMAT).format(record)
        self.assertRegex(line, r"^\n\d{4}-\d\d-\d\d \S+ supervisor: guest reset$")

    def test_a_clean_stop_logs_nothing(self):
        """Poweroff and halt exits leave no supervisor line."""
        for status in (supervisor.EXIT_POWEROFF, supervisor.EXIT_HALTED):
            with self.assertNoLogs(supervisor.LOG, "WARNING"):
                self._run([status])

    def test_abnormal_exits_are_logged_by_name_and_status(self):
        """A triple fault, a signal and an error exit each leave a line."""
        for status, expected in (
            (supervisor.EXIT_TRIPLE_FAULT, "triple-fault (status 3)"),
            (-signal.SIGABRT, "signal 6 (status -6)"),
            (4, "exit 4 (status 4)"),
        ):
            with self.assertLogs(supervisor.LOG, "WARNING") as logs:
                self._run([status])
            self.assertEqual(
                ["bhyve stopped abnormally: " + expected],
                [record.getMessage() for record in logs.records],
            )

    def test_a_requested_stop_is_not_logged_as_abnormal(self):
        """A guest killed by the driver's hard stop leaves no line."""
        with self.assertNoLogs(supervisor.LOG, "WARNING"):
            self._run([-signal.SIGKILL], stop_after=1)

    # Terminal exits: no respawn, and a truthful verdict.

    def test_poweroff_does_not_respawn(self):
        """A poweroff exit ends supervision with a poweroff verdict."""
        _sup, spawned, _reap = self._run([supervisor.EXIT_POWEROFF])
        self.assertEqual(1, len(spawned))
        self.assertEqual("poweroff", self._status())

    def test_halted_is_recorded_as_a_clean_stop(self):
        """A halted exit is recorded as halted."""
        self._run([supervisor.EXIT_HALTED])
        self.assertEqual("halted", self._status())

    def test_triple_fault_is_recorded_by_name(self):
        """A triple-fault exit is recorded by name."""
        self._run([supervisor.EXIT_TRIPLE_FAULT])
        self.assertEqual("triple-fault", self._status())

    def test_signal_death_is_recorded_as_a_signal(self):
        """A signalled child records "signal N", never bhyve's exit code N."""
        self._run([-signal.SIGKILL])
        self.assertEqual("signal 9", self._status())

    def test_unknown_exit_code_is_recorded_verbatim(self):
        """An unrecognised exit code is recorded as "exit N"."""
        self._run([42])
        self.assertEqual("exit 42", self._status())

    # Being asked to stop.

    def test_stopping_beats_a_reboot_exit(self):
        """A stop request wins even when the guest happened to exit 0."""
        # Otherwise a driver-initiated stop would race a reboot forever.
        _sup, spawned, _reap = self._run(
            [supervisor.EXIT_REBOOTED, supervisor.EXIT_POWEROFF], stop_after=1
        )
        self.assertEqual(1, len(spawned))

    def test_sigterm_forwards_to_the_guest_and_stops_supervising(self):
        """SIGTERM forwards to the child and marks the supervisor stopping."""
        sup = supervisor.Supervisor(self.state_dir, self.name, ["/bin/true"])
        sup.child = FakeChild(0)
        sup._on_term(signal.SIGTERM, None)
        self.assertTrue(sup.stopping)
        self.assertEqual([signal.SIGTERM], sup.child.signals)

    def test_sigusr1_is_the_hard_stop(self):
        """SIGUSR1 forwards SIGKILL to the child."""
        sup = supervisor.Supervisor(self.state_dir, self.name, ["/bin/true"])
        sup.child = FakeChild(0)
        sup._on_kill(signal.SIGUSR1, None)
        self.assertTrue(sup.stopping)
        self.assertEqual([signal.SIGKILL], sup.child.signals)

    # State files.

    def test_pidfile_is_written_then_removed(self):
        """The pidfile carries the child pid and never outlives the process."""
        pidfile = os.path.join(self.state_dir, "bhyve.pid")
        seen = {}

        sup = supervisor.Supervisor(self.state_dir, self.name, ["/bin/true"])

        def fake_popen(argv):
            """Hand out a child that powers off immediately."""
            child = FakeChild(supervisor.EXIT_POWEROFF)
            return child

        with (
            mock.patch.object(supervisor.subprocess, "Popen", side_effect=fake_popen),
            mock.patch.object(sup, "_reap"),
        ):
            original = sup._write

            def spy(path, text):
                """Record what is written to the pidfile."""
                original(path, text)
                if path == pidfile:
                    seen["pid"] = text.strip()

            sup._write = spy
            sup.run()

        self.assertEqual("4242", seen.get("pid"))
        self.assertFalse(
            os.path.exists(pidfile),
            "the pidfile is the liveness signal, so it must not outlive the process",
        )

    def test_state_files_are_not_world_writable(self):
        """State files are 0644: written by root, read unprivileged."""
        sup = supervisor.Supervisor(self.state_dir, self.name, ["/bin/true"])
        path = os.path.join(self.state_dir, "probe")
        sup._write(path, "x")
        self.assertEqual(0o644, os.stat(path).st_mode & 0o777)

    def test_stale_verdict_is_cleared_at_start(self):
        """A previous run's verdict cannot make a starting guest look crashed."""
        stale = os.path.join(self.state_dir, "exit.status")
        with open(stale, "w") as handle:
            handle.write("signal 9\n")
        self._run([supervisor.EXIT_POWEROFF])
        self.assertEqual("poweroff", self._status())


class SupervisorArgumentTestCase(unittest.TestCase):
    """Command-line parsing of the supervisor's own entry point."""

    def test_double_dash_is_stripped_from_the_bhyve_argv(self):
        """The "--" the caller passes does not reach bhyve's argv."""
        with mock.patch.object(supervisor.logging, "basicConfig") as config:
            with mock.patch.object(supervisor, "Supervisor") as klass:
                klass.return_value.run.return_value = 0
                supervisor.main(
                    [
                        "--state-dir",
                        "/tmp/x",
                        "--name",
                        "n",
                        "--",
                        "/usr/sbin/bhyve",
                        "-c",
                        "1",
                    ]
                )
        _args, _kwargs = klass.call_args
        self.assertEqual(["/usr/sbin/bhyve", "-c", "1"], klass.call_args[0][2])
        config.assert_called_once_with(
            stream=supervisor.sys.stderr, format=supervisor.LOG_FORMAT
        )

    def test_no_bhyve_argv_is_an_error(self):
        """An empty bhyve command line exits with a usage error."""
        # argparse prints its usage to stderr on the way out; swallow it so
        # a passing run stays quiet.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                supervisor.main(["--state-dir", "/tmp/x", "--name", "n"])
