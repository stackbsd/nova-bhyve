"""Nova ComputeDriver interface for bhyve."""

import functools
import os
import platform
import tempfile
import typing

import nova.conf
import os_resource_classes as orc
from nova.api.metadata import base as instance_metadata
from nova.compute import power_state, task_states
from nova.console import type as console_type
from nova.objects import diagnostics as diagnostics_obj
from nova.objects import fields as obj_fields
from os_brick_freebsd import iscsi as brick_iscsi
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import fileutils, timeutils, units

from nova import exception as nova_exception
from nova import utils as nova_utils
from nova.virt import configdrive, driver, hardware
from nova_bhyve import conf as bhyve_conf
from nova_bhyve import exception, imagecache, images, lifecycle, migration
from nova_bhyve import machine as machine_pkg
from nova_bhyve.storage import zfs

LOG = logging.getLogger(__name__)

CONF = nova.conf.CONF
_ = bhyve_conf

# same cap nova libvirt driver applies
MAX_CONSOLE_BYTES = 100 * units.Ki

# matches cinder-zfs-driver LOCAL_VOLUME_TYPE for when zvol device node can be
# used directly
LOCAL_VOLUME_TYPE = "zfs_local"
ISCSI_VOLUME_TYPE = "iscsi"

SNAPSHOT_PREFIX = "nova-snapshot-"
RESIZE_SNAPSHOT = "nova-resize"

EPHEMERAL_DISK = "eph%d"
SWAP_DISK = "swap"


# TODO: should describe, log_call, unsupported, tail_file be in a utils module?
def describe(value):
    """Return a description for a driver call argument."""
    if isinstance(value, (list, tuple)):
        return "[%s]" % ", ".join(describe(i) for i in value)
    try:
        for attr in ("uuid", "id"):
            ident = getattr(value, attr, None)
            if ident is not None:
                return "%s<%s>" % (type(value).__name__, ident)
        return repr(value)
    except Exception:
        return type(value).__name__


def log_call(method):
    """Wrap a driver method so each call is logged before the body runs."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        """Log the call, then run the wrapped method."""
        described = [describe(arg) for arg in args]
        described += ["%s=%s" % (key, describe(val)) for key, val in kwargs.items()]
        LOG.info("%s(%s)", method.__name__, ", ".join(described))
        return method(self, *args, **kwargs)

    return wrapper


def unsupported(method):
    """Wrap a method this driver does not implement, logs then raises."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        """Log the call, then raise NotImplementedError."""
        described = [describe(arg) for arg in args]
        described += ["%s=%s" % (key, describe(val)) for key, val in kwargs.items()]
        LOG.info(
            "%s(%s) not implemented",
            method.__name__,
            ", ".join(described),
        )
        raise NotImplementedError("nova_bhyve does not implement %s" % method.__name__)

    return wrapper


def tail_file(path, max_bytes):
    """Return up to the last ``max_bytes`` of a file, and how much was cut."""
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        skipped = max(0, handle.tell() - max_bytes)
        handle.seek(skipped)
        return handle.read(), skipped


def target_dev(mountpoint, connection_info):
    """Return the guest device name for an attachment."""
    device = mountpoint or (connection_info or {}).get("device")
    if not device:
        raise exception.BhyveDriverError(
            reason="no mountpoint given for the volume attachment"
        )
    return os.path.basename(device)


def disk_spec(connection_info, mountpoint):
    """Turn connection_info into DiskSpec."""
    volume_type = (connection_info or {}).get("driver_volume_type")
    if volume_type not in (LOCAL_VOLUME_TYPE, ISCSI_VOLUME_TYPE):
        raise exception.BhyveDriverError(
            reason="volume type %(actual)s is not supported; this driver "
            "attaches %(local)s and %(iscsi)s volumes"
            % {
                "actual": volume_type,
                "local": LOCAL_VOLUME_TYPE,
                "iscsi": ISCSI_VOLUME_TYPE,
            }
        )
    device_path = (connection_info.get("data") or {}).get("device_path")
    if not device_path:
        raise exception.BhyveDriverError(
            reason="the volume connection carries no device_path"
        )
    return machine_pkg.DiskSpec(
        device_path=device_path, target_dev=target_dev(mountpoint, connection_info)
    )


def tap_name(vif):
    """Return the name of the tap backing a vif."""
    # driver expects the l2 agent to create the taps with this name
    return "tap" + vif["id"][:11]


def nic_spec(vif):
    """Turn a vif into NicSpec."""
    if vif.get("type") != "bridge":
        raise exception.BhyveDriverError(
            reason="port %(port)s has vif_type %(type)s; this driver "
            "only supports bridge vifs"
            % {"port": vif.get("id"), "type": vif.get("type")}
        )
    details = vif.get("details") or {}
    bridge = details.get("bridge_name")
    if not bridge:
        raise exception.BhyveDriverError(
            reason="port %s carries no bridge_name in its vif_details; "
            "is the Neutron mechanism driver bound to it?" % vif.get("id")
        )
    return machine_pkg.NicSpec(
        tap_name=tap_name(vif), bridge=bridge, mac=vif["address"]
    )


def nic_specs(network_info):
    """Turn network_info into NicSpec."""
    return tuple(nic_spec(vif) for vif in network_info or [])


def sysctl(name):
    """Return a sysctl value as a string."""
    out, _err = processutils.execute("sysctl", "-n", name)
    return out.strip()


def snapshot_datasets(uuid, image_id):
    """Return the (snapshot, clone) names for an instance snapshot."""
    dataset = zfs.instance_dataset(uuid)
    suffix = SNAPSHOT_PREFIX + image_id
    return "%s@%s" % (dataset, suffix), "%s.%s" % (dataset, suffix)


def resize_snapshot(dataset):
    """Return the name of a zvol's resize revert point."""
    return "%s@%s" % (dataset, RESIZE_SNAPSHOT)


def instance_zvols(uuid):
    """Return the root, ephemeral and swap zvols of an instance."""
    return [zfs.instance_dataset(uuid), *zfs.local_disks(uuid)]


def wanted_local_disks(instance, block_device_info):
    """Return (name, size_bytes, target_dev) for each ephemeral and swap disk."""
    disks = []
    ephemerals = driver.block_device_info_get_ephemerals(block_device_info)
    for index, eph in enumerate(ephemerals):
        name = EPHEMERAL_DISK % index
        disks.append((name, eph["size"] * units.Gi, eph.get("device_name")))
    # a resize onto a flavor with ephemeral_gb brings no ephemeral mapping
    if not ephemerals and instance.flavor.ephemeral_gb:
        size = instance.flavor.ephemeral_gb * units.Gi
        disks.append((EPHEMERAL_DISK % 0, size, None))
    swap = driver.block_device_info_get_swap(block_device_info)
    disks.append((SWAP_DISK, swap["swap_size"] * units.Mi, swap["device_name"]))
    return [
        (name, size, os.path.basename(target or name))
        for name, size, target in disks
        if size
    ]


class BhyveDriver(driver.ComputeDriver):
    """Runs instances as bhyve VMs."""

    capabilities: typing.ClassVar[dict] = {
        "has_imagecache": True,
        "supports_evacuate": False,
        "supports_migrate_to_same_host": True,
        "supports_attach_interface": True,
        "supports_device_tagging": False,
        "supports_tagged_attach_interface": False,
        "supports_tagged_attach_volume": False,
        "supports_extend_volume": True,
        "supports_multiattach": False,
        "supports_trusted_certs": False,
        "supports_pcpus": False,
        "supports_accelerators": False,
        "supports_bfv_rescue": False,
        "supports_vtpm": False,
        "supports_secure_boot": False,
        "supports_socket_pci_numa_affinity": False,
        "supports_remote_managed_ports": False,
        "supports_address_space_passthrough": False,
        "supports_address_space_emulated": False,
        "supports_virtio_fs": False,
        "supports_mem_backing_file": False,
        "supports_ephemeral_encryption": False,
        "supports_ephemeral_encryption_luks": False,
        "supports_ephemeral_encryption_plain": False,
        "supports_image_type_aki": False,
        "supports_image_type_ami": False,
        "supports_image_type_ari": False,
        "supports_image_type_iso": False,
        "supports_image_type_qcow2": False,
        "supports_image_type_raw": True,
        "supports_image_type_vdi": False,
        "supports_image_type_vhd": False,
        "supports_image_type_vhdx": False,
        "supports_image_type_vmdk": False,
        "supports_image_type_ploop": False,
    }

    def __init__(self, virtapi, read_only=False):
        """Select the configured mechanism and record the node name."""
        super().__init__(virtapi)
        self._machine = machine_pkg.get_machine()
        self._iscsi = brick_iscsi.FreeBSDISCSIConnector(nova_utils.get_root_helper())
        self._image_cache_manager = imagecache.ImageCacheManager()
        self._lifecycle = lifecycle.LifecycleWatcher(self._machine, self.emit_event)
        self._nodename = CONF.host

    @log_call
    def init_host(self, host):
        """Connect the mechansism layer and check datasets exist on host."""
        dataset = CONF.bhyve.zfs_dataset
        if not dataset:
            raise exception.BhyveDriverError(
                reason="zfs_dataset is required and is not set"
            )

        self._machine.connect()

        for required in (dataset, zfs.images_dataset(), zfs.instances_dataset()):
            zfs.assert_dataset(required)
        LOG.info(
            "dataset %s and its images/ and instances/ children are present",
            dataset,
        )
        self._lifecycle.start()
        LOG.info("init_host complete on node %s", self._nodename)

    @log_call
    def cleanup_host(self, host):
        """Stop the lifecycle watcher and release the hypervisor connection."""
        self._lifecycle.stop()
        self._machine.close()

    def _host_resources(self):
        """Return the raw (unratioed) capacity."""
        return {
            "vcpus": int(sysctl("hw.ncpu")),
            "memory_mb": int(sysctl("hw.physmem")) // units.Mi,
            "local_gb": zfs.available_bytes() // units.Gi,
        }

    @log_call
    def get_available_resource(self, nodename):
        """Return the resource dictionary the resource tracker records."""
        resources = self._host_resources()
        resources.update(
            {
                "vcpus_used": 0,
                "memory_mb_used": 0,
                "local_gb_used": 0,
                "disk_available_least": resources["local_gb"],
                "hypervisor_type": obj_fields.HVType.BHYVE,
                "hypervisor_version": 0,
                "hypervisor_hostname": nodename,
                "cpu_info": "",
                "numa_topology": None,
                "supported_instances": [
                    (
                        obj_fields.Architecture.X86_64,
                        obj_fields.HVType.BHYVE,
                        obj_fields.VMMode.HVM,
                    ),
                ],
            }
        )
        return resources

    @log_call
    def update_provider_tree(self, provider_tree, nodename, allocations=None):
        """Publish inventory for this node."""
        resources = self._host_resources()
        inventory = {
            orc.VCPU: {
                "total": resources["vcpus"],
                "min_unit": 1,
                "max_unit": resources["vcpus"],
                "step_size": 1,
                "allocation_ratio": (
                    CONF.cpu_allocation_ratio or CONF.initial_cpu_allocation_ratio
                ),
                "reserved": CONF.reserved_host_cpus,
            },
            orc.MEMORY_MB: {
                "total": resources["memory_mb"],
                "min_unit": 1,
                "max_unit": resources["memory_mb"],
                "step_size": 1,
                "allocation_ratio": (
                    CONF.ram_allocation_ratio or CONF.initial_ram_allocation_ratio
                ),
                "reserved": CONF.reserved_host_memory_mb,
            },
            orc.DISK_GB: {
                "total": resources["local_gb"],
                "min_unit": 1,
                "max_unit": resources["local_gb"],
                "step_size": 1,
                "allocation_ratio": (
                    CONF.disk_allocation_ratio or CONF.initial_disk_allocation_ratio
                ),
                "reserved": CONF.reserved_host_disk_mb // units.Ki,
            },
        }
        LOG.info(
            "inventory for %(node)s: %(vcpu)s VCPU, %(mem)s MB, %(disk)s GB",
            {
                "node": nodename,
                "vcpu": resources["vcpus"],
                "mem": resources["memory_mb"],
                "disk": resources["local_gb"],
            },
        )
        provider_tree.update_inventory(nodename, inventory)

    @log_call
    def get_available_nodes(self, refresh=False):
        """Return single nodename managed by this driver instance."""
        return [self._nodename]

    @log_call
    def spawn(
        self,
        context,
        instance,
        image_meta,
        injected_files,
        admin_password,
        allocations,
        network_info=None,
        block_device_info=None,
        power_on=True,
        accel_info=None,
    ):
        """Boot an instance."""
        # check image is supported format
        images.check_acceptable(image_meta)

        # clone instance zvol from image snapshot, populating cache if needed
        flavor_bytes = instance.flavor.root_gb * units.Gi
        base, _virtual_size = images.ensure_cached(context, image_meta, flavor_bytes)
        target = zfs.instance_dataset(instance.uuid)
        zfs.clone(base, target)

        try:
            # the clone inherits image volsize, so resize it to the flavor
            if flavor_bytes and flavor_bytes > zfs.volsize_bytes(target):
                zfs.set_volsize(target, flavor_bytes)

            self._sync_local_disks(instance, block_device_info)

            # generate config drive
            if configdrive.required_by(instance):
                self._make_config_drive(
                    instance, injected_files, admin_password, network_info
                )

            # persist the config and power on
            self._define_domain(instance, block_device_info, network_info)
            if power_on:
                self._machine.start(instance.uuid)

        except Exception:
            LOG.exception("spawn of %s failed, rolling back", instance.uuid)
            self._teardown(instance.uuid)
            self._disconnect_volumes(block_device_info)
            raise

    def _boot_volumes(self, block_device_info):
        """Connect the volumes to define alongside the root disk at boot."""
        volumes = []
        for bdm in driver.block_device_info_get_mapping(block_device_info):
            connection_info = bdm.get("connection_info") or {}
            self._connect_volume(connection_info)
            volumes.append(disk_spec(connection_info, bdm.get("mount_device")))
        if volumes:
            LOG.info(
                "%d volume(s) present at boot: %s",
                len(volumes),
                ", ".join(v.target_dev for v in volumes),
            )
        return tuple(volumes)

    def _define_domain(self, instance, block_device_info=None, network_info=None):
        """Define the domain for an instance from its current flavor."""
        iso_path = os.path.join(
            CONF.instances_path, instance.uuid, machine_pkg.CONFIG_DRIVE_NAME
        )
        spec = machine_pkg.MachineSpec(
            uuid=instance.uuid,
            title=instance.display_name or instance.uuid,
            vcpus=instance.flavor.vcpus,
            mem_mib=instance.flavor.memory_mb,
            root_dev=zfs.device_path(zfs.instance_dataset(instance.uuid)),
            iso_path=iso_path if os.path.exists(iso_path) else None,
            local_disks=tuple(
                machine_pkg.DiskSpec(
                    device_path=zfs.device_path(
                        zfs.local_disk_dataset(instance.uuid, name)
                    ),
                    target_dev=target,
                )
                for name, _size, target in wanted_local_disks(
                    instance, block_device_info
                )
            ),
            volumes=self._boot_volumes(block_device_info),
            nics=nic_specs(network_info),
            vnc_port=(machine_pkg.VNC_PORT_AUTO if CONF.vnc.enabled else None),
        )
        self._machine.define(spec)

    @staticmethod
    def _sync_local_disks(instance, block_device_info):
        """Make an instance's ephemeral and swap zvols match its mappings."""
        uuid = instance.uuid
        wanted = {
            name: zfs.align_volsize(size)
            for name, size, _target in wanted_local_disks(instance, block_device_info)
        }
        for dataset in zfs.local_disks(uuid):
            name = dataset.rsplit(".", 1)[1]
            size = wanted.get(name)
            current = zfs.volsize_bytes(dataset)
            # swap holds nothing worth keeping so a new size means a new zvol
            if size is None or (name == SWAP_DISK and current != size):
                zfs.destroy_volume_and_snapshots(dataset)
            elif current < size:
                zfs.set_volsize(dataset, size)
        for name, size in wanted.items():
            dataset = zfs.local_disk_dataset(uuid, name)
            if not zfs.exists(dataset):
                zfs.create_volume(dataset, size)

    def _make_config_drive(
        self, instance, injected_files, admin_password, network_info
    ):
        """Build the instance's config drive ISO and return its path."""
        directory = os.path.join(CONF.instances_path, instance.uuid)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, machine_pkg.CONFIG_DRIVE_NAME)

        extra_md = {}
        if admin_password:
            extra_md["admin_pass"] = admin_password
        metadata = instance_metadata.InstanceMetadata(
            instance,
            content=injected_files,
            extra_md=extra_md,
            network_info=network_info,
        )

        LOG.info(
            "building config drive for %(uuid)s at %(path)s",
            {"uuid": instance.uuid, "path": path},
        )
        with configdrive.ConfigDriveBuilder(instance_md=metadata) as builder:
            builder.make_drive(path)
        return path

    @log_call
    def destroy(
        self,
        context,
        instance,
        network_info,
        block_device_info=None,
        destroy_disks=True,
        destroy_secrets=True,
    ):
        """Stop and delete an instance."""
        self._teardown(instance.uuid, destroy_disks=destroy_disks)
        self._disconnect_volumes(block_device_info)

    def _teardown(self, uuid, destroy_disks=True):
        """Remove every trace of an instance, skipping missing pieces."""
        timeout = CONF.bhyve.shutdown_timeout_graceful
        for description, step in (
            ("shutdown", lambda: self._machine.shutdown(uuid, timeout)),
            ("unplug", lambda: self._machine.unplug_nics(uuid)),
            ("undefine", lambda: self._machine.undefine(uuid)),
        ):
            try:
                step()
            except exception.BhyveDriverError as err:
                LOG.warning(
                    "%(what)s of %(uuid)s failed, continuing teardown: %(err)s",
                    {"what": description, "uuid": uuid, "err": err},
                )

        if destroy_disks:
            for dataset in instance_zvols(uuid):
                try:
                    zfs.destroy_volume_and_snapshots(dataset)
                except exception.BhyveDriverError as err:
                    LOG.warning(
                        "destroying %(dataset)s of %(uuid)s failed: %(err)s",
                        {"dataset": dataset, "uuid": uuid, "err": err},
                    )

            # delete the instance dir only if disks are destroyed
            directory = os.path.join(CONF.instances_path, uuid)
            if os.path.isdir(directory):
                LOG.info("removing instance directory %s", directory)
                for entry in os.scandir(directory):
                    if entry.is_file() or entry.is_symlink():
                        os.unlink(entry.path)
                os.rmdir(directory)

    @log_call
    def cleanup(
        self,
        context,
        instance,
        network_info,
        block_device_info=None,
        destroy_disks=True,
        migrate_data=None,
        destroy_vifs=True,
        destroy_secrets=True,
    ):
        """Nothing to do, destroy already removes everything."""

    @log_call
    def power_off(self, instance, timeout=0, retry_interval=0):
        """Stop an instance, hard-stop on timeout."""
        self._machine.shutdown(
            instance.uuid, timeout or CONF.bhyve.shutdown_timeout_graceful
        )

    @log_call
    def power_on(
        self,
        context,
        instance,
        network_info,
        block_device_info=None,
        accel_info=None,
        share_info=None,
    ):
        """Start a stopped instance."""
        for bdm in driver.block_device_info_get_mapping(block_device_info):
            connection_info = bdm.get("connection_info") or {}
            # sessions do not survive a host reboot and da numbering can change
            if self._connect_volume(connection_info):
                spec = disk_spec(connection_info, bdm.get("mount_device"))
                self._machine.detach_disk(instance.uuid, spec.target_dev)
                self._machine.attach_disk(instance.uuid, spec)
        self._machine.start(instance.uuid)

    @log_call
    def reboot(
        self,
        context,
        instance,
        network_info,
        reboot_type,
        block_device_info=None,
        bad_volumes_callback=None,
        accel_info=None,
        share_info=None,
    ):
        """Reboot an instance as a stop followed by a start."""
        # nothing in the driver will do a "reboot", always stop and start.
        # so any reboot will always be guest initiated
        uuid = instance.uuid
        if reboot_type == "SOFT":
            self._machine.shutdown(uuid, CONF.bhyve.shutdown_timeout_graceful)
        else:
            self._machine.destroy(uuid)
        self._machine.start(uuid)

    @log_call
    def plug_vifs(self, instance, network_info):
        """Plug the networking."""
        # direct mechanism just waits for the taps to appear
        self._machine.plug_nics(instance.uuid)

    @log_call
    def unplug_vifs(self, instance, network_info):
        """Unplug the networking."""
        # direct mechanism does nothing here
        self._machine.unplug_nics(instance.uuid)

    @log_call
    def default_root_device_name(self, instance, image_meta, root_bdm):
        """Get the virtio bus root device name."""
        return "/dev/" + machine_pkg.ROOT_TARGET_DEV

    @log_call
    def get_volume_connector(self, instance):
        """Get node volume connector info for cinder."""
        props = brick_iscsi.FreeBSDISCSIConnector.get_connector_properties(
            nova_utils.get_root_helper()
        )
        # host is compared by zfs driver to see if we can attach local
        props.update(
            {
                "host": CONF.host,
                "ip": CONF.my_block_storage_ip,
                "platform": platform.machine(),
                "os_type": "freebsd",
                "multipath": False,
            }
        )
        return props

    def _connect_volume(self, connection_info):
        """Log in to a remote volume and record its host device path."""
        if (connection_info or {}).get("driver_volume_type") != ISCSI_VOLUME_TYPE:
            return False
        data = connection_info["data"]
        data["device_path"] = self._iscsi.connect_volume(data)["path"]
        return True

    def _disconnect_volume(self, connection_info):
        """Log out of a remote volume."""
        if (connection_info or {}).get("driver_volume_type") != ISCSI_VOLUME_TYPE:
            return
        self._iscsi.disconnect_volume(connection_info["data"], None)

    def _disconnect_volumes(self, block_device_info):
        """Log out of every remote volume in a mapping, logging failures."""
        for bdm in driver.block_device_info_get_mapping(block_device_info):
            try:
                self._disconnect_volume(bdm.get("connection_info"))
            except Exception as err:
                LOG.warning(
                    "disconnecting %(target)s failed: %(err)s",
                    {"target": bdm.get("mount_device"), "err": err},
                )

    def _refuse_if_running(self, instance, verb, device):
        """Refuse a device operation against a running instance."""
        # only annoying thing with this is that the API accepts the request
        # but then the driver refuses, so from the user perspective nothing
        # happens, no error unless you check the logs
        if self._machine.state(instance.uuid)[0] == power_state.RUNNING:
            raise exception.BhyveDriverError(
                reason="cannot %(verb)s a %(device)s while %(uuid)s is "
                "running. Stop the instance, %(verb)s the %(device)s, then "
                "start it again."
                % {"verb": verb, "device": device, "uuid": instance.uuid}
            )

    @log_call
    def attach_volume(
        self,
        context,
        connection_info,
        instance,
        mountpoint,
        disk_bus=None,
        device_type=None,
        encryption=None,
    ):
        """Attach a Cinder volume to a stopped instance."""
        if encryption:
            raise exception.BhyveDriverError(
                reason="encrypted volumes are not supported"
            )
        self._refuse_if_running(instance, "attach", "volume")
        self._connect_volume(connection_info)
        try:
            self._machine.attach_disk(
                instance.uuid, disk_spec(connection_info, mountpoint)
            )
        except Exception:
            # leave no session behind for a disk nova will not know about
            self._disconnect_volume(connection_info)
            raise

    @log_call
    def detach_volume(
        self, context, connection_info, instance, mountpoint, encryption=None
    ):
        """Detach a Cinder volume from a stopped instance."""
        # don't guard with _refuse_if_running or it can make disks get stuck in
        # "detaching" if they were not attached in the first place
        self._machine.detach_disk(
            instance.uuid, target_dev(mountpoint, connection_info)
        )
        self._disconnect_volume(connection_info)

    @log_call
    def extend_volume(self, context, connection_info, instance, requested_size):
        """Refresh the host's view of a grown remote volume."""
        # a zvol attached directly already shows its new size and bhyve
        # reads disk sizes at start
        if (connection_info or {}).get("driver_volume_type") == ISCSI_VOLUME_TYPE:
            self._iscsi.extend_volume(connection_info["data"])

    @log_call
    def attach_interface(self, context, instance, image_meta, vif):
        """Attach a Neutron port to a stopped instance."""
        self._refuse_if_running(instance, "attach", "network interface")
        self._machine.attach_nic(instance.uuid, nic_spec(vif))

    @log_call
    def detach_interface(self, context, instance, vif):
        """Detach a Neutron port from a stopped instance."""
        self._machine.detach_nic(instance.uuid, tap_name(vif))

    @log_call
    def snapshot(self, context, instance, image_id, update_task_state):
        """Upload a snapshot of the root disk to glance."""
        uuid = instance.uuid
        dataset = zfs.instance_dataset(uuid)
        if not zfs.exists(dataset):
            raise nova_exception.InstanceNotFound(instance_id=uuid)

        image_format = CONF.bhyve.snapshot_image_format
        snapshot_name, clone = snapshot_datasets(uuid, image_id)
        metadata = self._snapshot_metadata(context, instance, image_id, image_format)

        # clean any previous attempt
        self._cleanup_snapshot(uuid, image_id)

        update_task_state(task_state=task_states.IMAGE_PENDING_UPLOAD)
        zfs.snapshot(dataset, SNAPSHOT_PREFIX + image_id)
        try:
            zfs.clone(snapshot_name, clone)
            LOG.info(
                "snapshotting %(uuid)s as image %(image)s (%(fmt)s), reading %(clone)s",
                {"uuid": uuid, "image": image_id, "fmt": image_format, "clone": clone},
            )
            update_task_state(
                task_state=task_states.IMAGE_UPLOADING,
                expected_state=task_states.IMAGE_PENDING_UPLOAD,
            )
            images.upload_snapshot(
                context, image_id, metadata, zfs.device_path(clone), image_format
            )
            LOG.info(
                "snapshot of %(uuid)s uploaded as %(image)s",
                {"uuid": uuid, "image": image_id},
            )
        finally:
            self._cleanup_snapshot(uuid, image_id)

    @staticmethod
    def _snapshot_metadata(context, instance, image_id, image_format):
        """Generate metadata for a snapshot image."""
        # no need to handle kernel or ramdisk, we always boot with firmware
        snapshot = images.fetch_metadata(context, image_id)
        metadata = {
            "name": snapshot["name"],
            "status": "active",
            "disk_format": image_format,
            "container_format": "bare",
            "properties": {
                "image_location": "snapshot",
                "image_state": "available",
                "owner_id": instance.project_id,
            },
        }
        if instance.os_type:
            metadata["properties"]["os_type"] = instance.os_type
        return metadata

    @staticmethod
    def _cleanup_snapshot(uuid, image_id):
        """Drop a snapshot."""
        snapshot_name, clone = snapshot_datasets(uuid, image_id)
        for step in (
            lambda: zfs.destroy_volume(clone),
            lambda: zfs.destroy_deferred(snapshot_name),
        ):
            try:
                step()
            except exception.BhyveDriverError as err:
                LOG.warning(
                    "cleaning up the snapshot of "
                    "%(uuid)s for image %(image)s failed: %(err)s",
                    {"uuid": uuid, "image": image_id, "err": err},
                )

    @log_call
    def manage_image_cache(self, context, all_instances):
        """Remove cached images that are no longer used."""
        self._image_cache_manager.update(context, all_instances)

    @log_call
    def migrate_disk_and_power_off(
        self,
        context,
        instance,
        dest,
        flavor,
        network_info,
        block_device_info=None,
        timeout=0,
        retry_interval=0,
    ):
        """Stop the instance, snapshot its disks and send them to dest."""
        uuid = instance.uuid
        dataset = zfs.instance_dataset(uuid)
        current = zfs.volsize_bytes(dataset)
        target = flavor.root_gb * units.Gi
        if target and target < current:
            raise nova_exception.InstanceFaultRollback(
                inner_exception=exception.BhyveDriverError(
                    reason="cannot resize %(uuid)s down to a %(new)s GiB root "
                    "disk from %(old)s GiB now, shrinking is not supported"
                    % {"uuid": uuid, "new": flavor.root_gb, "old": current // units.Gi}
                )
            )

        ephemerals = driver.block_device_info_get_ephemerals(block_device_info)
        ephemeral_gb = (
            sum(eph["size"] for eph in ephemerals) or instance.flavor.ephemeral_gb
        )
        if flavor.ephemeral_gb < ephemeral_gb:
            raise nova_exception.InstanceFaultRollback(
                inner_exception=exception.BhyveDriverError(
                    reason="cannot resize %(uuid)s down to %(new)s GiB of "
                    "ephemeral disk from %(old)s GiB now, shrinking is not "
                    "supported"
                    % {"uuid": uuid, "new": flavor.ephemeral_gb, "old": ephemeral_gb}
                )
            )

        running = self._machine.state(uuid)[0] == power_state.RUNNING
        self._machine.shutdown(uuid, timeout or CONF.bhyve.shutdown_timeout_graceful)

        for zvol in instance_zvols(uuid):
            snapshot_name = resize_snapshot(zvol)
            if zfs.exists(snapshot_name):
                LOG.warning(
                    "%s replacing existing resize revert point",
                    snapshot_name,
                )
                zfs.destroy_deferred(snapshot_name)
            zfs.snapshot(zvol, RESIZE_SNAPSHOT)

        if dest != self.get_host_ip_addr():
            self._send_instance(uuid, dest, running)
            self._disconnect_volumes(block_device_info)

        return jsonutils.dumps(
            {"root_volsize_bytes": current, "revert_snapshot": resize_snapshot(dataset)}
        )

    def _send_instance(self, uuid, dest, restart):
        """Copy an instance's disks and files to another host."""
        LOG.info("sending %(uuid)s to %(dest)s", {"uuid": uuid, "dest": dest})
        try:
            for zvol in instance_zvols(uuid):
                if zvol == zfs.instance_dataset(uuid):
                    name = migration.ROOT_DISK
                else:
                    name = zvol.rsplit(".", 1)[1]
                # swap holds nothing worth keeping and finish_migration remakes it
                if name == SWAP_DISK:
                    continue
                migration.send_volume(dest, uuid, name, resize_snapshot(zvol))
            for name in migration.INSTANCE_FILES:
                path = os.path.join(CONF.instances_path, uuid, name)
                if os.path.exists(path):
                    migration.send_file(dest, uuid, path)
        except Exception as err:
            LOG.exception("sending %s failed, keeping it here", uuid)
            try:
                migration.discard(dest, uuid)
            except Exception as cleanup_err:
                LOG.warning(
                    "could not discard %(uuid)s on %(dest)s: %(err)s",
                    {"uuid": uuid, "dest": dest, "err": cleanup_err},
                )
            for zvol in instance_zvols(uuid):
                zfs.destroy_deferred(resize_snapshot(zvol))
            if restart:
                self._machine.start(uuid)
            raise nova_exception.InstanceFaultRollback(inner_exception=err) from err

    @log_call
    def finish_migration(
        self,
        context,
        migration,
        instance,
        disk_info,
        network_info,
        image_meta,
        resize_instance,
        allocations,
        block_device_info=None,
        power_on=True,
    ):
        """Finish the resize or migration and start the instance again."""
        # instance.flavor is already the new flavor, grow the disk
        uuid = instance.uuid
        if resize_instance:
            target = instance.flavor.root_gb * units.Gi
            if target:
                zfs.set_volsize(zfs.instance_dataset(uuid), target)
        self._sync_local_disks(instance, block_device_info)

        # update definition on disk and power on
        self._define_domain(instance, block_device_info, network_info)
        if power_on:
            self._machine.start(uuid)

    @log_call
    def confirm_migration(self, context, migration, instance, network_info):
        """Drop the revert points, or the whole source copy after a move."""
        if instance.host != CONF.host:
            self._teardown(instance.uuid)
            return
        for zvol in instance_zvols(instance.uuid):
            zfs.destroy_deferred(resize_snapshot(zvol))

    @log_call
    def finish_revert_migration(
        self,
        context,
        instance,
        network_info,
        migration,
        block_device_info=None,
        power_on=True,
    ):
        """Put the instance back exactly as it was before the resize."""
        uuid = instance.uuid

        # might not be needed but doesn't hurt
        self._machine.shutdown(uuid, CONF.bhyve.shutdown_timeout_graceful)

        # no snapshot means we didnt get that far originally so skip it
        for zvol in instance_zvols(uuid):
            snapshot_name = resize_snapshot(zvol)
            if not zfs.exists(snapshot_name):
                LOG.info("%s has no revert snapshot", zvol)
                continue
            zfs.rollback(zvol, RESIZE_SNAPSHOT)
            zfs.destroy_deferred(snapshot_name)
        self._sync_local_disks(instance, block_device_info)

        self._define_domain(instance, block_device_info, network_info)
        if power_on:
            self._machine.start(uuid)

    @log_call
    def check_instance_shared_storage_local(self, context, instance):
        """Write a token in the instance directory for shared storage check."""
        directory = os.path.join(CONF.instances_path, instance.uuid)
        if not os.path.isdir(directory):
            return None
        fd, token = tempfile.mkstemp(dir=directory)
        os.close(fd)
        LOG.info("wrote shared storage token %s", token)
        return {"filename": token}

    @log_call
    def check_instance_shared_storage_remote(self, context, data):
        """Check instance directory for a shared storage token."""
        return os.path.exists(data["filename"])

    @log_call
    def check_instance_shared_storage_cleanup(self, context, data):
        """Remove the shared storage token from the instance directory."""
        fileutils.delete_if_exists(data["filename"])

    @log_call
    def get_info(self, instance, use_cache=True):
        """Get instance power state."""
        state, reason = self._machine.state(instance.uuid)
        LOG.info(
            "%(uuid)s is in state %(state)s (%(reason)s)",
            {"uuid": instance.uuid, "state": state, "reason": reason},
        )
        return hardware.InstanceInfo(state=state)

    @log_call
    def list_instances(self):
        """Return the UUIDs of every instance defined on this node."""
        return self._machine.list_uuids()

    @log_call
    def list_instance_uuids(self):
        """Return the UUIDs of every instance defined on this node."""
        return self._machine.list_uuids()

    @log_call
    def instance_exists(self, instance):
        """Report whether an instance is defined on this node."""
        return instance.uuid in self._machine.list_uuids()

    @log_call
    def get_host_ip_addr(self):
        """Return this host's configured IP address."""
        return CONF.my_ip

    @log_call
    def get_host_uptime(self):
        """Get the host uptime."""
        out, _err = processutils.execute("uptime")
        return out.strip()

    @unsupported
    def swap_volume(self, *args, **kwargs):
        """Volume swap is unsupported."""

    @unsupported
    def volume_snapshot_create(self, *args, **kwargs):
        """Volume snapshots are unsupported."""

    @unsupported
    def volume_snapshot_delete(self, *args, **kwargs):
        """Volume snapshots are unsupported."""

    @unsupported
    def cache_image(self, *args, **kwargs):
        """Image pre-caching is unsupported."""

    @unsupported
    def live_migration(self, *args, **kwargs):
        """Live migration is unsupported."""

    @unsupported
    def check_can_live_migrate_destination(self, *args, **kwargs):
        """Live migration is unsupported."""

    @unsupported
    def check_can_live_migrate_source(self, *args, **kwargs):
        """Live migration is unsupported."""

    @unsupported
    def pre_live_migration(self, *args, **kwargs):
        """Live migration is unsupported."""

    @unsupported
    def post_live_migration_at_destination(self, *args, **kwargs):
        """Live migration is unsupported."""

    # Suspend, pause, rescue, evacuate.
    @unsupported
    def pause(self, *args, **kwargs):
        """Pause is unsupported."""

    @unsupported
    def unpause(self, *args, **kwargs):
        """Pause is unsupported."""

    @unsupported
    def suspend(self, *args, **kwargs):
        """Suspend is unsupported."""

    @unsupported
    def resume(self, *args, **kwargs):
        """Suspend is unsupported."""

    @unsupported
    def rescue(self, *args, **kwargs):
        """Rescue is unsupported."""

    @unsupported
    def unrescue(self, *args, **kwargs):
        """Rescue is unsupported."""

    @unsupported
    def rebuild(self, *args, **kwargs):
        """Rebuild is unsupported."""

    @unsupported
    def trigger_crash_dump(self, *args, **kwargs):
        """Crash dump triggering is unsupported."""

    @unsupported
    def set_admin_password(self, *args, **kwargs):
        """Setting the admin password on a running guest is unsupported."""

    @log_call
    def get_console_output(self, context, instance):
        """Return tail of the instance's console log."""
        console_log = self._machine.describe(instance.uuid).get("console_log")
        if not console_log:
            raise nova_exception.ConsoleNotAvailable()
        if not os.path.exists(console_log):
            return b""
        data, skipped = tail_file(console_log, MAX_CONSOLE_BYTES)
        if skipped:
            LOG.info(
                "truncated console log for %(uuid)s: %(skipped)d bytes ignored",
                {"uuid": instance.uuid, "skipped": skipped},
            )
        return data

    @unsupported
    def get_serial_console(self, *args, **kwargs):
        """Not implemented, there is no interactive serial console."""

    @log_call
    def get_vnc_console(self, context, instance):
        """Get the VNC console connection."""
        port = self._machine.describe(instance.uuid).get("vnc_port")
        if not port:
            raise nova_exception.ConsoleTypeUnavailable(console_type="vnc")
        return console_type.ConsoleVNC(
            host=CONF.vnc.server_proxyclient_address, port=port
        )

    @unsupported
    def get_spice_console(self, *args, **kwargs):
        """SPICE is unsupported."""

    @unsupported
    def get_mks_console(self, *args, **kwargs):
        """MKS is unsupported."""

    @unsupported
    def get_diagnostics(self, *args, **kwargs):
        """Legacy diagnostics is not implemented."""

    @log_call
    def get_instance_diagnostics(self, instance):
        """Report bhyve statistics as nova diagnostics."""
        details = self._machine.describe(instance.uuid)
        stats = details.get("stats")
        if not stats:
            raise NotImplementedError(
                "the configured bhyve mechanism does not supply statistics"
            )

        state, _reason = self._machine.state(instance.uuid)
        diags = diagnostics_obj.Diagnostics(
            state=power_state.STATE_MAP[state],
            config_drive=configdrive.required_by(instance),
            hypervisor="bhyve",
            hypervisor_os="freebsd",
        )
        if instance.launched_at:
            launched = timeutils.normalize_time(instance.launched_at)
            diags.uptime = int(timeutils.delta_seconds(launched, timeutils.utcnow()))
        memory = diagnostics_obj.MemoryDiagnostics(maximum=details.get("mem_mib"))
        if "memory_resident_bytes" in stats:
            memory.used = stats["memory_resident_bytes"] // units.Mi
        diags.memory_details = memory
        for cpu in stats.get("cpus", []):
            diags.add_cpu(id=cpu["id"], time=cpu["time"])
        return diags

    @unsupported
    def get_host_cpu_stats(self, *args, **kwargs):
        """Host CPU statistics are unsupported."""

    @unsupported
    def block_stats(self, *args, **kwargs):
        """Per-disk statistics are unsupported."""

    @unsupported
    def get_all_volume_usage(self, *args, **kwargs):
        """Volume usage accounting is unsupported."""

    # Host administration.
    @unsupported
    def host_power_action(self, *args, **kwargs):
        """Host power actions are unsupported."""

    @unsupported
    def host_maintenance_mode(self, *args, **kwargs):
        """Host maintenance mode is unsupported."""

    @unsupported
    def set_host_enabled(self, *args, **kwargs):
        """Host availability is unsupported."""

    @unsupported
    def quiesce(self, *args, **kwargs):
        """Filesystem quiesce is unsupported."""

    @unsupported
    def unquiesce(self, *args, **kwargs):
        """Filesystem quiesce is unsupported."""
