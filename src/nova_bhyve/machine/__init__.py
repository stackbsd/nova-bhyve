"""bhyve mechanism layer."""

import abc
import dataclasses

# only affects name returned by the API
ROOT_TARGET_DEV = "vda"

# allocate a free vnc port automatically
VNC_PORT_AUTO = 0


@dataclasses.dataclass(frozen=True)
class DiskSpec:
    """A block device attached to an instance."""

    device_path: str
    target_dev: str


@dataclasses.dataclass(frozen=True)
class NicSpec:
    """A network interface attached to an instance."""

    tap_name: str
    bridge: str
    mac: str


@dataclasses.dataclass(frozen=True)
class MachineSpec:
    """Full configuration of an instance."""

    uuid: str
    title: str
    vcpus: int
    mem_mib: int
    root_dev: str
    iso_path: str | None = None
    volumes: tuple[DiskSpec, ...] = ()
    nics: tuple[NicSpec, ...] = ()
    vnc_port: int | None = None


class Machine(metaclass=abc.ABCMeta):
    """Lifecycle operations on bhyve Virtual Machines."""

    @abc.abstractmethod
    def connect(self):
        """Open the connection to the hypervisor."""

    @abc.abstractmethod
    def close(self):
        """Release the connection to the hypervisor."""

    @abc.abstractmethod
    def define(self, spec):
        """Define an instance from a MachineSpec."""

    @abc.abstractmethod
    def start(self, uuid):
        """Boot an instance."""

    @abc.abstractmethod
    def shutdown(self, uuid, timeout):
        """Request an instance stop, destroy after timeout."""

    @abc.abstractmethod
    def destroy(self, uuid):
        """Stop an instance immediately."""

    @abc.abstractmethod
    def undefine(self, uuid):
        """Remove an instance definition and its NVRAM."""

    @abc.abstractmethod
    def attach_disk(self, uuid, disk):
        """Add a disk to an instance."""

    @abc.abstractmethod
    def detach_disk(self, uuid, target_dev):
        """Remove a disk from an instance."""

    @abc.abstractmethod
    def attach_nic(self, uuid, nic):
        """Add a network interface to an instance."""

    @abc.abstractmethod
    def detach_nic(self, uuid, tap_name):
        """Remove a network interface from an instance."""

    @abc.abstractmethod
    def plug_nics(self, uuid):
        """Create the instance taps."""

    @abc.abstractmethod
    def unplug_nics(self, uuid):
        """Destroy the instance taps."""

    @abc.abstractmethod
    def state(self, uuid):
        """Return ``(power_state, reason)`` for an instance."""

    @abc.abstractmethod
    def list_uuids(self):
        """List the uuids for every instance."""

    @abc.abstractmethod
    def describe(self, uuid):
        """Return runtime details of an instance."""


def get_machine():
    """Return the configured mechanism implementation."""
    # currently on direct mechanism, libvirt in future
    from nova_bhyve.machine import direct as direct_machine

    return direct_machine.DirectMachine()
