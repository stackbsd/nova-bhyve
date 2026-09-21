"""The direct-bhyve implementation."""

import dataclasses
import json
import os
import re
import shutil
import time

from nova.compute import power_state
from oslo_concurrency import lockutils, processutils
from oslo_log import log as logging

from nova.virt.bhyve import privsep
from nova_bhyve import conf, exception, machine

LOG = logging.getLogger(__name__)

CONF = conf.CONF

VMM_DIR = "/dev/vmm"

SPEC_FILE = "machine.json"
BHYVE_PID = "bhyve.pid"
SUPERVISOR_PID = "supervisor.pid"
EXIT_STATUS = "exit.status"
CONSOLE_LOG = "console.log"
NVRAM_FILE = "nvram.fd"

# anything not listed here will be considered a crash
CLEAN_EXITS = ("poweroff", "halted")

STOP_POLL = 1.0

# how long to wait after launching the supervisor for bhyve to be running
START_WAIT_SECONDS = 30
START_POLL = 0.2

# TODO: should these all be CONF items? (vnc + nic wait)
VNC_PORT_BASE = 5900
FBUF_SLOT = 29
TABLET_SLOT = 30

NIC_WAIT_SECONDS = 60


# TODO: should this be called get_instance_state_dir ?
def uuid_dir(uuid):
    """Return an instance's state directory."""
    return os.path.join(CONF.instances_path, uuid)


# header to match each vcpu block in stats output
STATS_VCPU_HEADER = re.compile(r"^vcpu(\d+) stats:$")

STATS_RUNTIME = "vcpu total runtime"
STATS_RESIDENT = "Resident memory"


def parse_stats(text):
    """Parse the ``bhyvectl --get-stats`` output."""
    if not text:
        return None
    cpus = {}
    resident = None
    current = None
    for line in text.splitlines():
        header = STATS_VCPU_HEADER.match(line.strip())
        if header:
            current = int(header.group(1))
            continue
        name, _sep, value = line.rstrip().rpartition("\t")
        name = name.strip()
        if not name or not value.lstrip("-").isdigit():
            continue
        if name == STATS_RUNTIME and current is not None:
            cpus[current] = int(value)
        elif name == STATS_RESIDENT:
            resident = int(value)
    stats = {}
    if cpus:
        stats["cpus"] = [{"id": vcpu, "time": cpus[vcpu]} for vcpu in sorted(cpus)]
    if resident is not None:
        stats["memory_resident_bytes"] = resident
    return stats or None


# TODO: this should also be a common helper?
def pid_alive(pid):
    """Check if there is a running process for a pid."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    # ESRCH means process has gone
    except ProcessLookupError:
        return False
    # EPERM means its running but belongs to somebody else
    except PermissionError:
        return True
    return True


class DirectMachine(machine.Machine):
    """Direct machine lifecycle operations (using bhyve/bhyvectl)."""

    PROBE_NAME = "00000000-0000-0000-0000-000000000000"

    def connect(self):
        """Check permissions and privsep-helper is working."""
        # there is no daemon to connect since we manage the VMs directly
        # use vm_stats as a test to check privsep is working
        try:
            privsep.vm_stats(self.PROBE_NAME)
        except Exception as err:
            raise exception.BhyveDriverError(
                reason='the privileged helper is not usable, so the "direct" '
                "mechanism cannot run guests. Check [nova_sys_admin] "
                "helper_command in nova.conf and the sudoers entry for "
                "privsep-helper. Underlying error: %s" % err
            ) from err
        LOG.info("privileged helper ok")

    def close(self):
        """Nothing to do on this mechanism."""

    @staticmethod
    def _read_spec(uuid):
        """Return an instance's MachineSpec, or None if it is not defined."""
        path = os.path.join(uuid_dir(uuid), SPEC_FILE)
        try:
            with open(path) as handle:
                raw = json.load(handle)
        except (OSError, ValueError):
            return None
        volumes = tuple(machine.DiskSpec(**v) for v in raw.pop("volumes", ()))
        nics = tuple(machine.NicSpec(**n) for n in raw.pop("nics", ()))
        return machine.MachineSpec(volumes=volumes, nics=nics, **raw)

    @staticmethod
    def _write_spec(spec):
        """Persist a MachineSpec atomically into the instance directory."""
        directory = uuid_dir(spec.uuid)
        os.makedirs(directory, exist_ok=True)
        raw = dataclasses.asdict(spec)
        path = os.path.join(directory, SPEC_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(raw, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.rename(tmp, path)

    @staticmethod
    def _read_pid(uuid, filename):
        """Return the pid recorded in one of the instance's pidfiles, or None."""
        try:
            with open(os.path.join(uuid_dir(uuid), filename)) as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _read_exit(uuid):
        """Return the supervisor stored exit string, or None."""
        try:
            with open(os.path.join(uuid_dir(uuid), EXIT_STATUS)) as handle:
                return handle.read().strip()
        except OSError:
            return None

    @staticmethod
    def _vmm_present(uuid):
        """Report whether the instance's /dev/vmm object exists."""
        return os.path.exists(os.path.join(VMM_DIR, uuid))

    def _build_argv(self, spec):
        """Render a MachineSpec as a bhyve(8) command line."""
        # order is cdrom, then root disk, then volumes (similar to libvirt)
        # disks should be in the order you want the guest to find them
        argv = [
            "/usr/sbin/bhyve",
            "-c",
            str(spec.vcpus),
            "-m",
            str(spec.mem_mib),
            "-u",  # RTC keeps UTC
            "-H",  # yield the vcpu on HLT
            "-P",  # exit on PAUSE
            "-s",
            "0:0,hostbridge",
        ]

        directory = uuid_dir(spec.uuid)
        argv += [
            "-l",
            "bootrom,%s,%s"
            % (CONF.bhyve.bootrom_path, os.path.join(directory, NVRAM_FILE)),
        ]
        argv += ["-s", "1:0,lpc"]

        slot = 2
        if spec.iso_path:
            argv += ["-s", "%d:0,ahci,cd:%s" % (slot, spec.iso_path)]
            slot += 1
        argv += ["-s", "%d:0,virtio-blk,%s" % (slot, spec.root_dev)]
        slot += 1
        for volume in spec.volumes:
            argv += ["-s", "%d:0,virtio-blk,%s" % (slot, volume.device_path)]
            slot += 1

        for nic in spec.nics:
            argv += ["-s", "%d:0,virtio-net,%s,mac=%s" % (slot, nic.tap_name, nic.mac)]
            slot += 1

        if spec.vnc_port:
            argv += [
                "-s",
                "%d:0,fbuf,tcp=%s:%d"
                % (FBUF_SLOT, CONF.vnc.server_listen, spec.vnc_port),
            ]
            argv += ["-s", "%d:0,xhci,tablet" % TABLET_SLOT]

        # serial console goes to bhyve's stdout for get_console_output
        argv += ["-l", "com1,stdio"]

        # might be needed when nesting bhyve under KVM
        if CONF.bhyve.ignore_unimplemented_msr:
            argv.append("-w")

        argv.append(spec.uuid)
        return argv

    def _vnc_port_for(self, uuid):
        """Get the instance VNC port."""
        existing = self._read_spec(uuid)
        if existing and existing.vnc_port:
            return existing.vnc_port
        used = set()
        for other in self.list_uuids():
            spec = self._read_spec(other)
            if spec and spec.vnc_port:
                used.add(spec.vnc_port)
        port = VNC_PORT_BASE
        while port in used:
            port += 1
        return port

    @lockutils.synchronized("nova-bhyve-define")
    def define(self, spec):
        """Write instance config and UEFI variables to disk."""
        if self.state(spec.uuid)[0] == power_state.RUNNING:
            raise exception.MachineError(
                uuid=spec.uuid,
                reason="refusing to redefine %s while it is running; stop it "
                "first" % spec.uuid,
            )

        if spec.vnc_port == machine.VNC_PORT_AUTO:
            spec = dataclasses.replace(spec, vnc_port=self._vnc_port_for(spec.uuid))
            LOG.info(
                "allocating vnc port %(port)d for %(uuid)s",
                {"uuid": spec.uuid, "port": spec.vnc_port},
            )

        directory = uuid_dir(spec.uuid)
        os.makedirs(directory, exist_ok=True)

        # copy the nvram template if it exists
        nvram = os.path.join(directory, NVRAM_FILE)
        if not os.path.exists(nvram):
            shutil.copyfile(CONF.bhyve.nvram_template, nvram)

        LOG.info(
            "defining %(uuid)s (%(vcpus)s vcpu, %(mem)s MiB, root=%(root)s, iso=%(iso)s)",
            {
                "uuid": spec.uuid,
                "vcpus": spec.vcpus,
                "mem": spec.mem_mib,
                "root": spec.root_dev,
                "iso": spec.iso_path,
            },
        )
        self._write_spec(spec)

    def start(self, uuid):
        """Boot an instance under a supervisor."""
        spec = self._read_spec(uuid)
        if spec is None:
            raise exception.MachineError(uuid=uuid, reason="domain is not defined")
        if self.state(uuid)[0] == power_state.RUNNING:
            LOG.info("%s is already running", uuid)
            return

        directory = uuid_dir(uuid)

        # vmm object left by a previous failure would make this start fail
        if self._vmm_present(uuid):
            LOG.warning(
                "%s vmm reclaiming before starting",
                uuid,
            )
            privsep.reap_vm(uuid)

        # taps must exist before bhyve's argv references them
        # expect the l2 agent create them for us
        self._await_nics(spec)

        # clear exit status from previous run
        try:
            os.unlink(os.path.join(directory, EXIT_STATUS))
        except FileNotFoundError:
            pass

        argv = self._build_argv(spec)
        LOG.info("starting %s", uuid)
        LOG.debug(
            "argv for %(uuid)s: %(argv)s",
            {"uuid": uuid, "argv": " ".join(argv)},
        )
        privsep.start_domain(
            directory, uuid, argv, console_log=os.path.join(directory, CONSOLE_LOG)
        )
        self._wait_for_running(uuid)

    def _wait_for_running(self, uuid):
        """Wait for the instance to be running."""
        deadline = time.time() + START_WAIT_SECONDS
        while True:
            state, reason = self.state(uuid)
            if state == power_state.RUNNING:
                return
            if self._read_exit(uuid) is not None:
                raise exception.MachineError(
                    uuid=uuid,
                    reason="exited while starting (%s)" % reason,
                )
            if time.time() > deadline:
                raise exception.MachineError(
                    uuid=uuid,
                    reason="did not start in %ss" % START_WAIT_SECONDS,
                )
            time.sleep(START_POLL)

    def shutdown(self, uuid, timeout):
        """Request an instance stop, destroy after timeout."""
        # SIGTERM to bhyve is an ACPI poweroff
        if self.state(uuid)[0] != power_state.RUNNING:
            LOG.info("%s is not running", uuid)
            return
        LOG.info(
            "ACPI shutdown of %(uuid)s (timeout %(t)ss)",
            {"uuid": uuid, "t": timeout},
        )
        privsep.stop_domain(uuid_dir(uuid), uuid, graceful=True)

        # if a guest ignores ACPI, it will never stop so we must destroy
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.state(uuid)[0] != power_state.RUNNING:
                LOG.info("%s stopped gracefully", uuid)
                return
            time.sleep(STOP_POLL)
        LOG.warning(
            "%(uuid)s did not stop within %(t)ss, destroying",
            {"uuid": uuid, "t": timeout},
        )
        self.destroy(uuid)

    def destroy(self, uuid):
        """Stop an instance immediately."""
        if self.state(uuid)[0] == power_state.RUNNING:
            LOG.info("destroying %s", uuid)
            privsep.stop_domain(uuid_dir(uuid), uuid, graceful=False)
            deadline = time.time() + CONF.bhyve.destroy_timeout
            while time.time() < deadline:
                if self.state(uuid)[0] != power_state.RUNNING:
                    break
                time.sleep(STOP_POLL)
        # remove the vmm object
        privsep.reap_vm(uuid)

    def undefine(self, uuid):
        """Remove an instance's definition and its UEFI variables."""
        privsep.reap_vm(uuid)

        directory = uuid_dir(uuid)
        for name in (SPEC_FILE, NVRAM_FILE, EXIT_STATUS):
            try:
                os.unlink(os.path.join(directory, name))
            except FileNotFoundError:
                pass
        LOG.info("undefined %s", uuid)

    def attach_disk(self, uuid, disk):
        """Add a disk to a stopped instance."""
        spec = self._require_stopped(uuid, "attach", "disk")
        if any(v.target_dev == disk.target_dev for v in spec.volumes):
            LOG.info(
                "%(uuid)s already has a disk at "
                "%(target)s, leaving it alone",
                {"uuid": uuid, "target": disk.target_dev},
            )
            return
        LOG.info(
            "attaching %(path)s as %(target)s on %(uuid)s",
            {"path": disk.device_path, "target": disk.target_dev, "uuid": uuid},
        )
        self._write_spec(dataclasses.replace(spec, volumes=(*spec.volumes, disk)))

    def detach_disk(self, uuid, target_dev):
        """Remove a disk from a stopped instance."""
        spec = self._read_spec(uuid)
        if spec is None:
            raise exception.MachineError(uuid=uuid, reason="domain is not defined")

        # check if the disk already got detached
        kept = tuple(v for v in spec.volumes if v.target_dev != target_dev)
        if len(kept) == len(spec.volumes):
            LOG.info(
                "%(uuid)s has no disk at %(target)s, nothing to detach",
                {"uuid": uuid, "target": target_dev},
            )
            return

        self._require_stopped(uuid, "detach", "disk")
        LOG.info(
            "detaching %(target)s from %(uuid)s",
            {"target": target_dev, "uuid": uuid},
        )
        self._write_spec(dataclasses.replace(spec, volumes=kept))

    def attach_nic(self, uuid, nic):
        """Add a network interface to a stopped instance."""
        spec = self._require_stopped(uuid, "attach", "network interface")
        if any(n.tap_name == nic.tap_name for n in spec.nics):
            LOG.info(
                "%(uuid)s already has %(tap)s",
                {"uuid": uuid, "tap": nic.tap_name},
            )
            return
        LOG.info(
            "attaching %(tap)s (%(mac)s) on %(uuid)s",
            {"tap": nic.tap_name, "mac": nic.mac, "uuid": uuid},
        )
        self._write_spec(dataclasses.replace(spec, nics=(*spec.nics, nic)))

    def detach_nic(self, uuid, tap_name):
        """Remove a network interface from a stopped instance."""
        spec = self._read_spec(uuid)
        if spec is None:
            raise exception.MachineError(uuid=uuid, reason="domain is not defined")

        # check if the nic already got detached
        kept = tuple(n for n in spec.nics if n.tap_name != tap_name)
        if len(kept) == len(spec.nics):
            LOG.info(
                "%(uuid)s has no nic %(tap)s so nothing to detach",
                {"uuid": uuid, "tap": tap_name},
            )
            return

        self._require_stopped(uuid, "detach", "network interface")
        LOG.info("detaching %(tap)s from %(uuid)s", {"tap": tap_name, "uuid": uuid})
        self._write_spec(dataclasses.replace(spec, nics=kept))

    def _require_stopped(self, uuid, verb, device):
        """Get the spec of a stopped instance, or raise."""
        spec = self._read_spec(uuid)
        if spec is None:
            raise exception.MachineError(uuid=uuid, reason="domain is not defined")
        if self.state(uuid)[0] == power_state.RUNNING:
            raise exception.MachineError(
                uuid=uuid,
                reason="cannot %(verb)s a %(device)s while running. Stop the "
                "instance, %(verb)s the %(device)s, then start it again."
                % {"verb": verb, "device": device},
            )
        return spec

    @staticmethod
    def _nic_present(tap_name):
        """Report whether a tap interface exists on the host."""
        try:
            processutils.execute("/sbin/ifconfig", tap_name)
        except processutils.ProcessExecutionError:
            return False
        return True

    def _await_nics(self, spec):
        """Wait for every tap in the spec to exist, or raise."""
        deadline = time.time() + NIC_WAIT_SECONDS
        for nic in spec.nics:
            while not self._nic_present(nic.tap_name):
                if time.time() > deadline:
                    raise exception.MachineError(
                        uuid=spec.uuid,
                        reason="tap %s never appeared: the networking "
                        "agent must create it when the neutron port "
                        "converges, so check that the neutron services "
                        "are running and the port went ACTIVE" % nic.tap_name,
                    )
                time.sleep(1)

    def plug_nics(self, uuid):
        """Wait for the instance's taps to exist."""
        spec = self._read_spec(uuid)
        if spec is None:
            raise exception.MachineError(uuid=uuid, reason="domain is not defined")
        self._await_nics(spec)

    def unplug_nics(self, uuid):
        """Nothing to do on this mechanism, l2 agent should own the tap lifecycle."""

    def state(self, uuid):
        """Return ``(power_state, reason)`` for an instance."""
        if self._read_spec(uuid) is None:
            return power_state.NOSTATE, "undefined"

        # check bhyve pidfile first
        if pid_alive(self._read_pid(uuid, BHYVE_PID)):
            return power_state.RUNNING, "bhyve process running"

        # check supervisor exit reason
        reason = self._read_exit(uuid)
        if reason is not None:
            if reason in CLEAN_EXITS:
                return power_state.SHUTDOWN, "guest %s" % reason
            return power_state.CRASHED, "guest stopped: %s" % reason

        # vmm is there but no supervisor or bhyve, must have crashed
        if self._vmm_present(uuid):
            return (power_state.CRASHED, "vmm object left behind with no supervisor")

        return power_state.SHUTDOWN, "not running"

    def list_uuids(self):
        """Return the UUIDs of every instance defined on this node."""
        try:
            entries = os.listdir(CONF.instances_path)
        except OSError:
            return []
        return [
            name
            for name in entries
            if os.path.isfile(os.path.join(CONF.instances_path, name, SPEC_FILE))
        ]

    def describe(self, uuid):
        """Return runtime details from parsed bhyvectl stats."""
        if self._read_spec(uuid) is None:
            return {}
        spec = self._read_spec(uuid)
        details = {
            "pid": self._read_pid(uuid, BHYVE_PID),
            "supervisor_pid": self._read_pid(uuid, SUPERVISOR_PID),
            "console_log": os.path.join(uuid_dir(uuid), CONSOLE_LOG),
            "tap_names": [nic.tap_name for nic in spec.nics],
            "vnc_port": spec.vnc_port,
            "mem_mib": spec.mem_mib,
        }
        stats = parse_stats(privsep.vm_stats(uuid))
        if stats:
            details["stats"] = stats
        return details
