"""Supervisor for a bhyve process."""

import argparse
import errno
import os
import signal
import subprocess
import sys

BHYVECTL = "/usr/sbin/bhyvectl"

# bhyve(8) EXIT STATUS. Only REBOOTED means "start it again".
EXIT_REBOOTED = 0
EXIT_POWEROFF = 1
EXIT_HALTED = 2
EXIT_TRIPLE_FAULT = 3

FILE_MODE = 0o644


class Supervisor:
    """Supervisor for a bhyve process."""

    def __init__(self, state_dir, name, argv):
        """Set up supervisor state and file paths."""
        self.state_dir = state_dir
        self.name = name
        self.argv = argv
        self.child = None
        # Set by a signal: stop supervising, whatever the guest's exit says.
        self.stopping = False
        self.pidfile = os.path.join(state_dir, "bhyve.pid")
        self.statusfile = os.path.join(state_dir, "exit.status")

    def _on_term(self, _signum, _frame):
        """Send SIGTERM for ACPI shutdown."""
        self.stopping = True
        self._signal_child(signal.SIGTERM)

    def _on_kill(self, _signum, _frame):
        """Send SIGKILL for immediate stop."""
        self.stopping = True
        self._signal_child(signal.SIGKILL)

    def _signal_child(self, signum):
        """Forward a signal to child bhyve process."""
        if self.child is None or self.child.poll() is not None:
            return
        try:
            self.child.send_signal(signum)
        except OSError as err:
            if err.errno != errno.ESRCH:
                raise

    def _write(self, path, text):
        """Write state file."""
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        os.rename(tmp, path)

    def _record_exit(self, status):
        """Record how the guest stopped, as a single token state() can read."""
        # A negative value from subprocess means "killed by signal N", which
        # is a crash, not a shutdown.
        if status < 0:
            reason = "signal %d" % -status
        elif status == EXIT_POWEROFF:
            reason = "poweroff"
        elif status == EXIT_HALTED:
            reason = "halted"
        elif status == EXIT_TRIPLE_FAULT:
            reason = "triple-fault"
        elif status == EXIT_REBOOTED:
            reason = "rebooted"
        else:
            reason = "exit %d" % status
        self._write(self.statusfile, reason + "\n")

    def run(self):
        """Run the guest, re-executing bhyve on reboot unless stopping."""
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGUSR1, self._on_kill)

        # delete old state file
        try:
            os.unlink(self.statusfile)
        except FileNotFoundError:
            pass

        status = EXIT_POWEROFF
        while True:
            self.child = subprocess.Popen(self.argv)
            self._write(self.pidfile, "%d\n" % self.child.pid)
            try:
                status = self.child.wait()
            except KeyboardInterrupt:
                status = -signal.SIGINT
            finally:
                try:
                    os.unlink(self.pidfile)
                except FileNotFoundError:
                    pass

            if self.stopping:
                break
            if status != EXIT_REBOOTED:
                break

            self._reap()

        self._record_exit(status)
        self._reap()
        return 0

    def _reap(self):
        """Reclaim the /dev/vmm object."""
        if not os.path.exists(os.path.join("/dev/vmm", self.name)):
            return
        subprocess.call(
            [BHYVECTL, "--destroy", "--vm=%s" % self.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def main(argv=None):
    """Console entrypoint to run supervisor."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("bhyve_argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    bhyve_argv = args.bhyve_argv
    if bhyve_argv and bhyve_argv[0] == "--":
        bhyve_argv = bhyve_argv[1:]
    if not bhyve_argv:
        parser.error("no bhyve command line given")

    return Supervisor(args.state_dir, args.name, bhyve_argv).run()


if __name__ == "__main__":
    sys.exit(main())
