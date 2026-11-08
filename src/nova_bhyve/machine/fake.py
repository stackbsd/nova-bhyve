"""Fake bhyve mechanism."""

import contextlib
import os

from nova.compute import power_state
from oslo_log import log as logging

from nova_bhyve import exception
from nova_bhyve.machine import direct

LOG = logging.getLogger(__name__)

RUNNING_FILE = "fake.running"


def running_path(uuid):
    """Return the path of an instance's running marker."""
    return os.path.join(direct.uuid_dir(uuid), RUNNING_FILE)


class FakeMachine(direct.DirectMachine):
    """Machine that records power state without running bhyve."""

    def connect(self):
        """Warn that instances will not boot."""
        LOG.warning("fake mechanism in use, instances will not boot")

    @staticmethod
    def _vmm_present(uuid):
        """Report that no /dev/vmm object exists."""
        return False

    def start(self, uuid):
        """Check an instance's devices and mark it running."""
        spec = self._read_spec(uuid)
        if spec is None:
            raise exception.MachineError(uuid=uuid, reason="domain is not defined")
        if self.state(uuid)[0] == power_state.RUNNING:
            LOG.info("%s is already running", uuid)
            return

        self._await_nics(spec)
        paths = [spec.root_dev]
        if spec.iso_path:
            paths.append(spec.iso_path)
        paths += [disk.device_path for disk in (*spec.local_disks, *spec.volumes)]
        missing = [path for path in paths if not os.path.exists(path)]
        if missing:
            raise exception.MachineError(
                uuid=uuid,
                reason="devices missing: %s. Check the storage backend "
                "attached them before starting" % ", ".join(missing),
            )

        argv = " ".join(self._build_argv(spec))
        LOG.info("starting %s without bhyve", uuid)
        LOG.debug("argv for %(uuid)s: %(argv)s", {"uuid": uuid, "argv": argv})
        directory = direct.uuid_dir(uuid)
        with open(os.path.join(directory, direct.CONSOLE_LOG), "a") as handle:
            handle.write(argv + "\n")
        with open(running_path(uuid), "w"):
            pass

    def shutdown(self, uuid, timeout):
        """Mark an instance stopped."""
        self.destroy(uuid)

    def destroy(self, uuid):
        """Mark an instance stopped."""
        if self.state(uuid)[0] == power_state.RUNNING:
            LOG.info("stopping %s", uuid)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(running_path(uuid))

    def undefine(self, uuid):
        """Remove an instance's definition and its UEFI variables."""
        for name in (direct.SPEC_FILE, direct.NVRAM_FILE, RUNNING_FILE):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(os.path.join(direct.uuid_dir(uuid), name))
        LOG.info("undefined %s", uuid)

    def state(self, uuid):
        """Return ``(power_state, reason)`` for an instance."""
        if self._read_spec(uuid) is None:
            return power_state.NOSTATE, "undefined"
        if os.path.exists(running_path(uuid)):
            return power_state.RUNNING, "fake marked running"
        return power_state.SHUTDOWN, "not running"

    def describe(self, uuid):
        """Return runtime details without statistics."""
        spec = self._read_spec(uuid)
        if spec is None:
            return {}
        return {
            "console_log": os.path.join(direct.uuid_dir(uuid), direct.CONSOLE_LOG),
            "tap_names": [nic.tap_name for nic in spec.nics],
            "vnc_port": spec.vnc_port,
            "mem_mib": spec.mem_mib,
        }
