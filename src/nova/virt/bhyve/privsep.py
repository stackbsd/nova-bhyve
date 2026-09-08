"""Privileged operations for the bhyve driver."""

import os
import re
import signal
import sys

import nova.privsep
from oslo_concurrency import processutils
from oslo_log import log as logging

LOG = logging.getLogger(__name__)

# TODO: these are defined in multiple places across the codebase. we should reuse from the main pkg
BHYVE = "/usr/sbin/bhyve"
BHYVECTL = "/usr/sbin/bhyvectl"
DAEMON = "/usr/sbin/daemon"

VMM_DIR = "/dev/vmm"

# TODO: should this be a validate_uuid helper?
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")


def check_name(name):
    """Raise unless a name is shaped like an instance UUID."""
    if not UUID_RE.match(name):
        raise ValueError("refusing to act on %r: not an instance uuid" % name)
    return name


def check_state_dir(state_dir, name):
    """Raise unless a state directory is absolute and belongs to this VM."""
    check_name(name)
    if not os.path.isabs(state_dir) or os.path.basename(state_dir.rstrip("/")) != name:
        raise ValueError(
            "refusing to use %r as the state directory for %s" % (state_dir, name)
        )
    return state_dir


def pid_from(path):
    """Read a pidfile, returning None if it is absent or unreadable."""
    try:
        with open(path) as handle:
            return int(handle.read().strip())
    except (OSError, ValueError):
        return None


@nova.privsep.sys_admin_pctxt.entrypoint
def start_domain(state_dir, name, argv, console_log=None):
    """Start a guest under a nova_bhyve.supervisor process."""
    check_state_dir(state_dir, name)
    if not all(isinstance(a, str) for a in argv):
        raise ValueError("bhyve arguments must all be strings")

    cmd = [DAEMON, "-f", "-p", os.path.join(state_dir, "supervisor.pid")]
    if console_log:
        # the serial device is configured by the driver as stdio, so the log
        # from daemon(8) is literally the console log ;-)
        cmd += ["-o", console_log, "-m", "3", "-M", "0640"]
    cmd += [
        "--",
        sys.executable,
        "-m",
        "nova_bhyve.supervisor",
        "--state-dir",
        state_dir,
        "--name",
        name,
        "--",
        *argv,
    ]

    LOG.info("bhyve privsep: starting %(name)s under a supervisor", {"name": name})
    processutils.execute(*cmd)


@nova.privsep.sys_admin_pctxt.entrypoint
def stop_domain(state_dir, name, graceful=True):
    """Signal a running guest, returning whether there was one to signal."""
    check_state_dir(state_dir, name)

    # send SIGTERM to the supervisor (ACPI shutdown)
    sup_pid = pid_from(os.path.join(state_dir, "supervisor.pid"))
    if sup_pid:
        try:
            os.kill(sup_pid, signal.SIGTERM if graceful else signal.SIGUSR1)
            return True
        except ProcessLookupError:
            pass

    # supervisor is dead, try to kill bhyve directly
    pid = pid_from(os.path.join(state_dir, "bhyve.pid"))
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM if graceful else signal.SIGKILL)
    except ProcessLookupError:
        return False
    return True


@nova.privsep.sys_admin_pctxt.entrypoint
def reap_vm(name):
    """Reclaim a VM object left behind in /dev/vmm."""
    check_name(name)
    if not os.path.exists(os.path.join(VMM_DIR, name)):
        return False
    LOG.info("bhyve privsep: reclaiming the vmm object for %s", name)
    try:
        processutils.execute(BHYVECTL, "--destroy", "--vm=%s" % name)
    except processutils.ProcessExecutionError as err:
        LOG.warning(
            "bhyve privsep: could not reclaim %(name)s: %(err)s",
            {"name": name, "err": err},
        )
        return False
    return True


@nova.privsep.sys_admin_pctxt.entrypoint
def vm_stats(name):
    """Return bhyvectl's per-vcpu statistics as raw text, or None."""
    check_name(name)
    if not os.path.exists(os.path.join(VMM_DIR, name)):
        return None
    try:
        out, _err = processutils.execute(BHYVECTL, "--vm=%s" % name, "--get-stats")
    except processutils.ProcessExecutionError:
        return None
    return out
