"""Glance to ZFS image download and snapshot upload helpers."""

import os

from nova.i18n import _
from nova.image import glance
from oslo_concurrency import lockutils, processutils
from oslo_log import log as logging
from oslo_utils import imageutils, units
from oslo_utils.imageutils import format_inspector

from nova import exception as nova_exception
from nova_bhyve import conf
from nova_bhyve.storage import zfs

LOG = logging.getLogger(__name__)

CONF = conf.CONF

BASE_SNAPSHOT = "base"

LOCK_PREFIX = "nova-bhyve-image-"

SUPPORTED_FORMATS = ("raw", "qcow2")
SNAPSHOT_FORMATS = ("raw", "qcow2")

# need to write to zvol device in fixed size blocks
# use the same block size we aligned the zfs create to
WRITE_BLOCK = zfs.VOLSIZE_ALIGNMENT

# copied from nova for qemu-img info
# don't use this limit for qemu-img convert since that can take a while
QEMU_IMG_LIMITS = processutils.ProcessLimits(cpu_time=30, address_space=1 * units.Gi)


class AlignedDeviceWriter:
    """Aligned writes to a zvol device."""

    def __init__(self, handle, block=WRITE_BLOCK):
        """Buffer writes into aligned blocks on the given handle."""
        self._handle = handle
        self._block = block
        self._buffer = bytearray()
        self.written = 0
        self._closed = False

    def write(self, data):
        """Write data flushing whole blocks to the device."""
        self._buffer += data
        while len(self._buffer) >= self._block:
            chunk = bytes(self._buffer[: self._block])
            del self._buffer[: self._block]
            self._handle.write(chunk)
            self.written += len(chunk)
        return len(data)

    def close(self):
        """Flush remaining buffer and zero pad to block size."""
        if self._closed:
            return
        self._closed = True
        if self._buffer:
            tail = bytes(self._buffer).ljust(self._block, b"\0")
            self._handle.write(tail)
            self.written += len(tail)
            self._buffer = bytearray()
        self._handle.flush()
        os.fsync(self._handle.fileno())


def image_dataset(image_id):
    """Return the dataset of an image's cache zvol."""
    return "%s/%s" % (zfs.images_dataset(), image_id)


def base_snapshot(image_id):
    """Return the name of an image's @base snapshot."""
    return "%s@%s" % (image_dataset(image_id), BASE_SNAPSHOT)


def declared_format(image_meta):
    """Return the disk format Glance declares, or None."""
    if image_meta.obj_attr_is_set("disk_format"):
        return image_meta.disk_format
    return None


def check_acceptable(image_meta):
    """Raise unless this is an image the driver can boot."""
    disk_format = declared_format(image_meta)
    if disk_format not in SUPPORTED_FORMATS:
        raise nova_exception.ImageUnacceptable(
            image_id=image_meta.id,
            reason=_(
                "the bhyve driver supports disk_format %(supported)s, "
                "but this image is %(actual)s"
            )
            % {"supported": " and ".join(SUPPORTED_FORMATS), "actual": disk_format},
        )


def glance_size(image_meta):
    """Return the byte size Glance reports, or raise if it reports none."""
    size = None
    if image_meta.obj_attr_is_set("size"):
        size = image_meta.size
    if not size:
        raise nova_exception.ImageUnacceptable(
            image_id=image_meta.id,
            reason=_(
                "glance reports no size for this image, so the cache "
                "zvol cannot be sized"
            ),
        )
    return size


def check_flavor_fits(image_meta, virtual_size, flavor_bytes):
    """Refuse a flavor whose root disk is smaller than the image needs."""
    if flavor_bytes and flavor_bytes < virtual_size:
        raise nova_exception.FlavorDiskSmallerThanImage(
            flavor_size=flavor_bytes, image_size=virtual_size
        )


def qemu_img_info(path, image_id, disk_format=None):
    """Run ``qemu-img info`` and return parsed output."""
    cmd = [
        "env",
        "LC_ALL=C",
        "LANG=C",
        "qemu-img",
        "info",
        path,
        "--force-share",
        "--output=json",
    ]
    if disk_format is not None:
        cmd += ["-f", disk_format]
    try:
        out, _err = processutils.execute(*cmd, prlimit=QEMU_IMG_LIMITS)
    except processutils.ProcessExecutionError as err:
        if err.exit_code == -9:
            reason = _("qemu-img info was killed by its resource limits")
        else:
            reason = _("qemu-img info failed: %s") % err
        raise nova_exception.ImageUnacceptable(
            image_id=image_id, reason=reason
        ) from err
    return imageutils.QemuImgInfo(out, format="json")


def detect_format(path):
    """Inspect the format of the provided path."""
    with open(path, "rb") as handle:
        wrapper = format_inspector.InspectWrapper(handle)
        try:
            while handle.peek():
                wrapper.read(4096)
                if wrapper.formats:
                    break
        finally:
            wrapper.close()
    return wrapper.format


def inspect(path, image_meta):
    """Inspect a downloaded image and return its virtual size in bytes."""
    image_id = image_meta.id
    declared = declared_format(image_meta)

    try:
        if not format_inspector.get_inspector(declared):
            raise nova_exception.ImageUnacceptable(
                image_id=image_id, reason=_("no format inspector for %s") % declared
            )
        inspector = detect_format(path)
        inspector.safety_check()
        detected = str(inspector)
        if detected != declared:
            LOG.warning(
                "%(id)s image content is %(detected)s but disk_format says %(declared)s",
                {"id": image_id, "declared": declared, "detected": detected},
            )
            raise nova_exception.ImageUnacceptable(
                image_id=image_id,
                reason=_(
                    "image content is %(detected)s but disk_format says %(declared)s"
                )
                % {"detected": detected, "declared": declared},
            )
    except nova_exception.ImageUnacceptable:
        raise
    except format_inspector.SafetyCheckFailed as err:
        LOG.error(
            "%(id)s failed safety check: %(err)s",
            {"id": image_id, "err": err},
        )
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_("failed safety check: %s") % err,
        ) from err
    except format_inspector.ImageFormatError as err:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_("image content does not match disk_format %s") % declared,
        ) from err
    except Exception as err:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id, reason=_("image is not in a supported format: %s") % err
        ) from err

    # comapare the insector with qemu-img, do not allow backing or external files
    info = qemu_img_info(path, image_id, disk_format=declared)
    if info.file_format != declared:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_("qemu-img reports %(actual)s but disk_format says %(declared)s")
            % {"actual": info.file_format, "declared": declared},
        )

    if info.backing_file is not None:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id, reason=_("image is backed by %s") % info.backing_file
        )

    try:
        data_file = info.format_specific["data"]["data-file"]
    except (KeyError, TypeError, AttributeError):
        data_file = None
    if data_file is not None:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_("image has an external data file: %s") % data_file,
        )

    if not info.virtual_size:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_("qemu-img reports no virtual size for this image"),
        )

    LOG.info(
        "%(id)s inspected: format %(fmt)s, virtual size %(size)s bytes",
        {"id": image_id, "fmt": declared, "size": info.virtual_size},
    )
    return info.virtual_size


def is_cached(image_id):
    """Check if usable image cache entry exists."""
    dataset = image_dataset(image_id)
    if not zfs.exists(dataset):
        return False
    return base_snapshot(image_id) in zfs.snapshot_search(
        BASE_SNAPSHOT, root_dataset=dataset
    )


def get_cached_images():
    """Return the id of every image with a cache zvol."""
    return [os.path.basename(d) for d in zfs.get_volumes(zfs.images_dataset())]


def is_inuse(image_id):
    """Check if any zvol is cloned from an image's cache entry."""
    return is_cached(image_id) and bool(zfs.clones_of(base_snapshot(image_id)))


def remove_image(image_id):
    """Remove an image from the cache and report whether it was removed."""
    with lockutils.lock(LOCK_PREFIX + image_id, external=True):
        # recheck under the lock as a spawn may have cloned it since
        if is_inuse(image_id):
            return False
        destroy_cache(image_id)
        return True


def destroy_cache(image_id):
    """Remove an image from the cache."""
    zfs.destroy_deferred(base_snapshot(image_id))
    zfs.destroy_volume(image_dataset(image_id))


def ensure_cached(context, image_meta, flavor_bytes=None):
    """Populate the image cache if needed and return ``(base_snapshot, virtual_size)``."""
    image_id = image_meta.id
    with lockutils.lock(LOCK_PREFIX + image_id, external=True):
        if is_cached(image_id):
            virtual_size = zfs.volsize_bytes(image_dataset(image_id))
            LOG.info(
                "images: cache hit for %(id)s (%(size)s bytes)",
                {"id": image_id, "size": virtual_size},
            )
            check_flavor_fits(image_meta, virtual_size, flavor_bytes)
            return base_snapshot(image_id), virtual_size

        if zfs.exists(image_dataset(image_id)):
            LOG.warning(
                "%s has a cache volume but no @base "
                "snapshot, destroying and recreating",
                image_id,
            )
            destroy_cache(image_id)

        if declared_format(image_meta) == "raw":
            virtual_size = populate_raw(context, image_meta, flavor_bytes)
        else:
            virtual_size = populate_converted(context, image_meta, flavor_bytes)
        return base_snapshot(image_id), virtual_size


def snapshot_populated(image_id):
    """Snapshot a freshly populated cache zvol as @base."""
    zfs.snapshot(image_dataset(image_id), BASE_SNAPSHOT)
    LOG.info(
        "cached %(id)s as %(snap)s",
        {"id": image_id, "snap": base_snapshot(image_id)},
    )


def populate_raw(context, image_meta, flavor_bytes):
    """Stream a raw image into a cache zvol."""
    image_id = image_meta.id
    dataset = image_dataset(image_id)

    virtual_size = glance_size(image_meta)
    check_flavor_fits(image_meta, virtual_size, flavor_bytes)

    LOG.debug("raw stream for %s", image_id)
    LOG.info(
        "cache miss for %(id)s, streaming raw into %(dataset)s "
        "(%(size)s bytes)",
        {"id": image_id, "dataset": dataset, "size": virtual_size},
    )
    zfs.create_volume(dataset, virtual_size)

    try:
        device = zfs.device_path(dataset)
        with open(device, "wb", buffering=0) as handle:
            writer = AlignedDeviceWriter(handle)
            glance.API().download(context, image_id, data=writer)
            writer.close()
        LOG.info(
            "streamed %(written)s bytes into %(device)s",
            {"written": writer.written, "device": device},
        )
        snapshot_populated(image_id)
    except Exception:
        LOG.exception(
            "populating the cache for %s failed, removing the partial entry",
            image_id,
        )
        destroy_cache(image_id)
        raise
    return virtual_size


def populate_converted(context, image_meta, flavor_bytes):
    """Download a non-raw image and convert it into the cache zvol."""
    image_id = image_meta.id
    declared = declared_format(image_meta)
    dataset = image_dataset(image_id)

    scratch = CONF.bhyve.image_scratch_dir
    try:
        os.makedirs(scratch, exist_ok=True)
    except OSError as err:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_("cannot create the image scratch directory %(dir)s: %(err)s")
            % {"dir": scratch, "err": err},
        ) from err
    spool = os.path.join(scratch, "%s.download" % image_id)

    LOG.debug(
        "convert stream for %(id)s (%(fmt)s -> raw)",
        {"id": image_id, "fmt": declared},
    )
    LOG.info(
        "cache miss for %(id)s, downloading %(fmt)s to %(spool)s "
        "for conversion",
        {"id": image_id, "fmt": declared, "spool": spool},
    )

    created_volume = False
    try:
        with open(spool, "wb") as handle:
            glance.API().download(context, image_id, data=handle)
            handle.flush()
            os.fsync(handle.fileno())
        LOG.info(
            "downloaded %(bytes)s bytes to %(spool)s",
            {"bytes": os.path.getsize(spool), "spool": spool},
        )

        virtual_size = inspect(spool, image_meta)
        check_flavor_fits(image_meta, virtual_size, flavor_bytes)

        zfs.create_volume(dataset, virtual_size)
        created_volume = True
        device = zfs.device_path(dataset)

        LOG.info(
            "converting %(fmt)s -> raw into %(device)s",
            {"fmt": declared, "device": device},
        )
        processutils.execute(
            "qemu-img",
            "convert",
            "-t",
            "none",
            "-f",
            declared,
            "-O",
            "raw",
            "-n",
            "--target-is-zero",
            spool,
            device,
        )

        with open(device, "rb+", buffering=0) as handle:
            os.fsync(handle.fileno())

        snapshot_populated(image_id)
    except Exception:
        LOG.exception(
            "populating the cache for %s failed, removing the partial entry",
            image_id,
        )
        if created_volume:
            destroy_cache(image_id)
        raise
    finally:
        if os.path.exists(spool):
            LOG.info("removing spool file %s", spool)
            os.unlink(spool)
    return virtual_size


def fetch_metadata(context, image_id):
    """Get Glance metadata for an image."""
    return glance.API().get(context, image_id)


def upload_snapshot(context, image_id, metadata, device, image_format):
    """Upload a snapshot to Glance."""
    if image_format not in SNAPSHOT_FORMATS:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_(
                "cannot upload a snapshot as %(actual)s supported "
                "formats are %(supported)s"
            )
            % {"actual": image_format, "supported": " and ".join(SNAPSHOT_FORMATS)},
        )

    if image_format == "raw":
        LOG.info(
            "uploading %(id)s from %(device)s as raw",
            {"id": image_id, "device": device},
        )
        with open(device, "rb") as handle:
            glance.API().update(
                context, image_id, metadata, data=handle, purge_props=False
            )
        return

    scratch = CONF.bhyve.image_scratch_dir
    try:
        os.makedirs(scratch, exist_ok=True)
    except OSError as err:
        raise nova_exception.ImageUnacceptable(
            image_id=image_id,
            reason=_("cannot create the image scratch directory %(dir)s: %(err)s")
            % {"dir": scratch, "err": err},
        ) from err
    spool = os.path.join(scratch, "%s.snapshot" % image_id)

    try:
        LOG.info(
            "converting %(device)s -> %(fmt)s at %(spool)s",
            {"device": device, "fmt": image_format, "spool": spool},
        )
        processutils.execute(
            "qemu-img",
            "convert",
            "-t",
            "none",
            "-f",
            "raw",
            "-O",
            image_format,
            device,
            spool,
        )
        LOG.info(
            "uploading %(bytes)s bytes to glance as %(id)s",
            {"bytes": os.path.getsize(spool), "id": image_id},
        )
        with open(spool, "rb") as handle:
            glance.API().update(
                context, image_id, metadata, data=handle, purge_props=False
            )
    finally:
        if os.path.exists(spool):
            LOG.info("removing spool file %s", spool)
            os.unlink(spool)
