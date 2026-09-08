"""Nova ComputeDriver interface for bhyve."""

import functools
import os
import platform
import tempfile
import time
import typing

import nova.conf
import os_resource_classes as orc
from nova.api.metadata import base as instance_metadata
from nova.compute import power_state, task_states
from nova.console import type as console_type
from nova.objects import diagnostics as diagnostics_obj
from nova.objects import fields as obj_fields
from oslo_concurrency import processutils
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import fileutils, timeutils, units

from nova import exception as nova_exception
from nova.virt import configdrive, driver, hardware
from nova_bhyve import conf as bhyve_conf
from nova_bhyve import exception, images
from nova_bhyve import machine as machine_pkg
from nova_bhyve.storage import zfs

LOG = logging.getLogger(__name__)

CONF = nova.conf.CONF
_ = bhyve_conf

CONFIG_DRIVE_NAME = "config.iso"

# same cap nova libvirt driver applies
MAX_CONSOLE_BYTES = 100 * units.Ki

# matches cinder-zfs-driver LOCAL_VOLUME_TYPE for when zvol device node can be
# used directly
LOCAL_VOLUME_TYPE = "zfs_local"

# retry for instance destroy (zvol release is async)
BUSY_ATTEMPTS = 10
BUSY_BACKOFF = 1.0

SNAPSHOT_PREFIX = "nova-snapshot-"
RESIZE_SNAPSHOT = "nova-resize"


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
    # only the local zvol type is implemented currently
    volume_type = (connection_info or {}).get("driver_volume_type")
    if volume_type != LOCAL_VOLUME_TYPE:
        raise exception.BhyveDriverError(
            reason="volume type %(actual)s is not supported; this driver only "
            "attaches %(supported)s volumes, whose storage is on this "
            "same host" % {"actual": volume_type, "supported": LOCAL_VOLUME_TYPE}
        )
    device_path = (connection_info.get("data") or {}).get("device_path")
    if not device_path:
        raise exception.BhyveDriverError(
            reason="the volume connection carries no device_path"
        )
    return machine_pkg.DiskSpec(
        device_path=device_path, target_dev=target_dev(mountpoint, connection_info)
    )


def nic_specs(network_info):
    """Turn network_info into NicSpec."""
    nics = []
    for vif in network_info or []:
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
        nics.append(
            # driver expects the l2 agent to create the taps with this name
            machine_pkg.NicSpec(
                tap_name="tap" + vif["id"][:11], bridge=bridge, mac=vif["address"]
            )
        )
    return tuple(nics)


def sysctl(name):
    """Return a sysctl value as a string."""
    out, _err = processutils.execute("sysctl", "-n", name)
    return out.strip()


def snapshot_datasets(uuid, image_id):
    """Return the (snapshot, clone) names for an instance snapshot."""
    dataset = zfs.instance_dataset(uuid)
    suffix = SNAPSHOT_PREFIX + image_id
    return "%s@%s" % (dataset, suffix), "%s.%s" % (dataset, suffix)


def resize_snapshot(uuid):
    """Return the name of an instance's resize revert point."""
    return "%s@%s" % (zfs.instance_dataset(uuid), RESIZE_SNAPSHOT)


def retry_while_busy(what, step):
    """Run a zfs operation, retrying if busy."""
    for attempt in range(1, BUSY_ATTEMPTS + 1):
        try:
            step()
            return
        except exception.BhyveDriverError as err:
            if "busy" not in str(err) or attempt == BUSY_ATTEMPTS:
                raise
            LOG.info(
                "%(what)s failed because the dataset is "
                "busy, retrying (%(n)d/%(total)d)",
                {"what": what, "n": attempt, "total": BUSY_ATTEMPTS},
            )
            time.sleep(BUSY_BACKOFF)


class BhyveDriver(driver.ComputeDriver):
    """Runs instances as bhyve VMs."""

    capabilities: typing.ClassVar[dict] = {
        "has_imagecache": False,
        "supports_evacuate": False,
        "supports_migrate_to_same_host": True,
        "supports_attach_interface": False,
        "supports_device_tagging": False,
        "supports_tagged_attach_interface": False,
        "supports_tagged_attach_volume": False,
        "supports_extend_volume": False,
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
            "dataset %s and its images/ and instances/ "
            "children are present",
            dataset,
        )
        LOG.info("init_host complete on node %s", self._nodename)


    @log_call
    def cleanup_host(self, host):
        """Release the hypervisor connection."""
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
            "inventory for %(node)s: %(vcpu)s VCPU, "
            "%(mem)s MB, %(disk)s GB",
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
            LOG.exception(
                "spawn of %s failed, rolling back", instance.uuid
            )
            self._teardown(instance.uuid)
            raise

    @staticmethod
    def _boot_volumes(block_device_info):
        """Get the volumes to define alongside the root disk at boot."""
        volumes = []
        for bdm in driver.block_device_info_get_mapping(block_device_info):
            connection_info = bdm.get("connection_info") or {}
            mountpoint = bdm.get("mount_device")
            volumes.append(disk_spec(connection_info, mountpoint))
        if volumes:
            LOG.info(
                "%d volume(s) present at boot: %s",
                len(volumes),
                ", ".join(v.target_dev for v in volumes),
            )
        return tuple(volumes)

    def _define_domain(self, instance, block_device_info=None, network_info=None):
        """Define the domain for an instance from its current flavor."""
        iso_path = os.path.join(CONF.instances_path, instance.uuid, CONFIG_DRIVE_NAME)
        spec = machine_pkg.MachineSpec(
            uuid=instance.uuid,
            title=instance.display_name or instance.uuid,
            vcpus=instance.flavor.vcpus,
            mem_mib=instance.flavor.memory_mb,
            root_dev=zfs.device_path(zfs.instance_dataset(instance.uuid)),
            iso_path=iso_path if os.path.exists(iso_path) else None,
            volumes=self._boot_volumes(block_device_info),
            nics=nic_specs(network_info),
            vnc_port=(machine_pkg.VNC_PORT_AUTO if CONF.vnc.enabled else None),
        )
        self._machine.define(spec)

    def _make_config_drive(
        self, instance, injected_files, admin_password, network_info
    ):
        """Build the instance's config drive ISO and return its path."""
        directory = os.path.join(CONF.instances_path, instance.uuid)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, CONFIG_DRIVE_NAME)

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
                    "%(what)s of %(uuid)s failed, "
                    "continuing teardown: %(err)s",
                    {"what": description, "uuid": uuid, "err": err},
                )

        if destroy_disks:
            # need to delete snapshots before the root zvol
            self._destroy_instance_snapshots(uuid)
            self._destroy_root_zvol(uuid)

            # delete the instance dir only if disks are destroyed
            directory = os.path.join(CONF.instances_path, uuid)
            if os.path.isdir(directory):
                LOG.info("removing instance directory %s", directory)
                for entry in os.scandir(directory):
                    if entry.is_file() or entry.is_symlink():
                        os.unlink(entry.path)
                os.rmdir(directory)


    @staticmethod
    def _destroy_instance_snapshots(uuid):
        """Remove any snapshots and snapshot clones for an instance."""
        for snapshot_name in zfs.snapshots_of(zfs.instance_dataset(uuid)):
            try:
                for clone in zfs.clones_of(snapshot_name):
                    LOG.info(
                        "destroying leftover snapshot clone %s", clone
                    )
                    zfs.destroy_volume(clone)
                zfs.destroy_deferred(snapshot_name)
            except exception.BhyveDriverError as err:
                LOG.warning(
                    "could not remove snapshot %(snap)s "
                    "of %(uuid)s: %(err)s",
                    {"snap": snapshot_name, "uuid": uuid, "err": err},
                )

    @staticmethod
    def _destroy_root_zvol(uuid):
        """Destroy an instance root zvol."""
        dataset = zfs.instance_dataset(uuid)
        try:
            retry_while_busy(
                "destroying %s" % dataset, lambda: zfs.destroy_volume(dataset)
            )
        except exception.BhyveDriverError as err:
            LOG.warning(
                "destroying the root zvol of %(uuid)s failed: %(err)s",
                {"uuid": uuid, "err": err},
            )

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
        # host is compared by zfs driver to see if we can attach local
        return {
            "host": CONF.host,
            "platform": platform.machine(),
            "os_type": "freebsd",
        }

    def _refuse_if_running(self, instance, verb):
        """Refuse a volume operation against a running instance."""
        # only annoying thing with this is that the API accepts the request
        # but then the driver refuses, so from the user perspective nothing
        # happens, no error unless you check the logs
        if self._machine.state(instance.uuid)[0] == power_state.RUNNING:
            raise exception.BhyveDriverError(
                reason="cannot %(verb)s a volume while %(uuid)s is running as "
                "bhyve cannot hot-plug disks. Stop the instance, "
                "%(verb)s the volume, then start it again."
                % {"verb": verb, "uuid": instance.uuid}
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
        self._refuse_if_running(instance, "attach")
        self._machine.attach_disk(instance.uuid, disk_spec(connection_info, mountpoint))

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
                "snapshotting %(uuid)s as image %(image)s "
                "(%(fmt)s), reading %(clone)s",
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
        """Stop the instance and take snapshot for same-host resize only."""
        uuid = instance.uuid
        if dest != self.get_host_ip_addr():
            raise nova_exception.InstanceFaultRollback(
                inner_exception=exception.BhyveDriverError(
                    reason="cannot move %(uuid)s to %(dest)s: this driver "
                    "does not currently support migrate of local zvols "
                    "to another host, only a same-host resize is possible"
                    % {"uuid": uuid, "dest": dest}
                )
            )

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

        self._machine.shutdown(uuid, timeout or CONF.bhyve.shutdown_timeout_graceful)

        snapshot_name = resize_snapshot(uuid)
        if zfs.exists(snapshot_name):
            LOG.warning(
                "%s replacing existing resize revert point",
                snapshot_name,
            )
            zfs.destroy_deferred(snapshot_name)
        zfs.snapshot(dataset, RESIZE_SNAPSHOT)

        return jsonutils.dumps(
            {"root_volsize_bytes": current, "revert_snapshot": snapshot_name}
        )

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
        """Finish the same-host resize and start the instance again."""
        # instance.flavor is already the new flavor, grow the disk
        uuid = instance.uuid
        if resize_instance:
            target = instance.flavor.root_gb * units.Gi
            if target:
                zfs.set_volsize(zfs.instance_dataset(uuid), target)

        # update definition on disk and power on
        self._define_domain(instance, block_device_info, network_info)
        if power_on:
            self._machine.start(uuid)

    @log_call
    def confirm_migration(self, context, migration, instance, network_info):
        """Drop the revert point snapshot for same-host resize."""
        zfs.destroy_deferred(resize_snapshot(instance.uuid))

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
        dataset = zfs.instance_dataset(uuid)
        snapshot_name = resize_snapshot(uuid)

        # might not be needed but doesn't hurt
        self._machine.shutdown(uuid, CONF.bhyve.shutdown_timeout_graceful)

        if zfs.exists(snapshot_name):
            retry_while_busy(
                "rolling %s back" % dataset,
                lambda: zfs.rollback(dataset, RESIZE_SNAPSHOT),
            )
            zfs.destroy_deferred(snapshot_name)
        else:
            # no snapshot means we didnt get that far originally
            # safe to continue
            LOG.warning(
                "%s has no revert snapshot; "
                "reverting the domain definition only",
                uuid,
            )

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
    def attach_interface(self, *args, **kwargs):
        """Interface hot-plug is unsupported."""

    @unsupported
    def detach_interface(self, *args, **kwargs):
        """Interface hot-plug is unsupported."""

    @unsupported
    def swap_volume(self, *args, **kwargs):
        """Volume swap is unsupported."""

    @unsupported
    def extend_volume(self, *args, **kwargs):
        """Volume extend is unsupported."""

    @unsupported
    def volume_snapshot_create(self, *args, **kwargs):
        """Volume snapshots are unsupported."""

    @unsupported
    def volume_snapshot_delete(self, *args, **kwargs):
        """Volume snapshots are unsupported."""

    @unsupported
    def cache_image(self, *args, **kwargs):
        """Nova's image cache manager is unsupported."""

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
    def manage_image_cache(self, *args, **kwargs):
        """Nova's image cache manager is unsupported."""

    @unsupported
    def quiesce(self, *args, **kwargs):
        """Filesystem quiesce is unsupported."""

    @unsupported
    def unquiesce(self, *args, **kwargs):
        """Filesystem quiesce is unsupported."""
