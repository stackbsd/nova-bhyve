"""ZFS zvol backend for bhyve compute driver."""

import os

from oslo_concurrency import processutils
from oslo_log import log as logging

from nova_bhyve import conf, exception

LOG = logging.getLogger(__name__)

CONF = conf.CONF

ZVOL_DEV_DIR = "/dev/zvol"

# zfs create -V needs a multiple of dataset volblocksize
VOLSIZE_ALIGNMENT = 1024 * 1024


def run(*args, dataset=None):
    """Run a zfs command, or raise."""
    try:
        out, _err = processutils.execute("zfs", *args)
    except processutils.ProcessExecutionError as err:
        raise exception.ZFSCommandError(
            command=args[0],
            dataset=dataset or args[-1],
            reason=(err.stderr or str(err)).strip(),
        ) from err
    return out


def root():
    """Return the configured root dataset, or raise if it is unset."""
    dataset = CONF.bhyve.zfs_dataset
    if not dataset:
        raise exception.BhyveDriverError(
            reason="zfs_dataset is required and is not set"
        )
    return dataset


def assert_under_root(dataset):
    """Raise unless a dataset lies under the configured root dataset."""
    top = root()
    if dataset != top and not dataset.startswith(top + "/"):
        raise exception.BhyveDriverError(
            reason="refusing to operate on %s: not under [bhyve] "
            "zfs_dataset %s" % (dataset, top)
        )


def align_volsize(size_bytes):
    """Round a byte count up to a size zfs will accept for -V."""
    blocks = -(-int(size_bytes) // VOLSIZE_ALIGNMENT)
    return blocks * VOLSIZE_ALIGNMENT


def images_dataset():
    """Return the dataset for cached image zvols."""
    return "%s/images" % root()


def instances_dataset():
    """Return the dataset for per-instance root zvols."""
    return "%s/instances" % root()


def instance_dataset(uuid):
    """Return the dataset for an instance root zvol."""
    return "%s/%s" % (instances_dataset(), uuid)


def device_path(dataset):
    """Return the device node of a zvol."""
    return os.path.join(ZVOL_DEV_DIR, dataset)


def exists(dataset):
    """Check if a dataset or snapshot exists."""
    try:
        processutils.execute("zfs", "list", "-H", "-o", "name", dataset)
    except processutils.ProcessExecutionError as err:
        if "does not exist" in (err.stderr or ""):
            return False
        raise exception.ZFSCommandError(
            command="list", dataset=dataset, reason=(err.stderr or str(err)).strip()
        ) from err
    return True


def snapshot_search(name, root_dataset=None):
    """Return the full names of snapshots called ``name`` under a dataset."""
    # clone origin might be under a different dataset so search by name
    out = run(
        "list", "-H", "-t", "snapshot", "-o", "name", "-r", root_dataset or root()
    )
    suffix = "@%s" % name
    return [line for line in out.split() if line.endswith(suffix)]


def available_bytes(dataset=None):
    """Get free bytes for a dataset."""
    out = run("list", "-Hpo", "avail", dataset or root())
    return int(out.strip())


def volsize_bytes(dataset):
    """Get provisioned size of a zvol in bytes."""
    out = run("list", "-Hpo", "volsize", dataset)
    return int(out.strip())


def assert_dataset(dataset):
    """Assert that a dataset exists, raises if not."""
    # The driver never creates filesystem datasets at runtime; the parents
    # are made once during host provisioning.
    if not exists(dataset):
        raise exception.BhyveDriverError(
            reason="dataset %s does not exist; create it during host "
            "provisioning (see the host requirements in the README)" % dataset
        )


def create_volume(dataset, size_bytes, sparse=True):
    """Create a zvol."""
    assert_under_root(dataset)
    args = ["create"]
    if sparse:
        args.append("-s")
    args += ["-V", str(align_volsize(size_bytes)), dataset]
    LOG.info(
        "creating zvol %(dataset)s of %(size)s bytes",
        {"dataset": dataset, "size": align_volsize(size_bytes)},
    )
    run(*args, dataset=dataset)


def snapshot(dataset, name):
    """Snapshot a dataset and return the full name."""
    assert_under_root(dataset)
    full = "%s@%s" % (dataset, name)
    LOG.info("snapshotting %s", full)
    run("snapshot", full, dataset=full)
    return full


def clone(snapshot_name, dataset):
    """Clone a snapshot into a new zvol."""
    assert_under_root(dataset)
    LOG.info("cloning %(src)s -> %(dst)s", {"src": snapshot_name, "dst": dataset})
    run("clone", snapshot_name, dataset, dataset=dataset)


def set_volsize(dataset, size_bytes):
    """Grow a zvol."""
    assert_under_root(dataset)
    target = align_volsize(size_bytes)
    current = volsize_bytes(dataset)
    if target < current:
        raise exception.BhyveDriverError(
            reason="refusing to shrink %(dataset)s from %(current)s to "
            "%(target)s bytes"
            % {"dataset": dataset, "current": current, "target": target}
        )
    if target == current:
        return
    LOG.info(
        "growing %(dataset)s to %(size)s bytes",
        {"dataset": dataset, "size": target},
    )
    run("set", "volsize=%d" % target, dataset, dataset=dataset)


def destroy_deferred(snapshot_name):
    """Delete a snapshot with ``zfs destroy -d``."""
    # deferred so that the original is kept until last clone is deleted
    assert_under_root(snapshot_name.split("@", 1)[0])
    if not exists(snapshot_name):
        LOG.info("snapshot %s already gone", snapshot_name)
        return
    LOG.info("deferred-destroying snapshot %s", snapshot_name)
    run("destroy", "-d", snapshot_name, dataset=snapshot_name)


def destroy_volume(dataset):
    """Delete a zvol."""
    assert_under_root(dataset)
    if not exists(dataset):
        LOG.info("volume %s already gone", dataset)
        return
    LOG.info("destroying volume %s", dataset)
    run("destroy", dataset, dataset=dataset)


def snapshots_of(dataset):
    """Return all the snapshot full names for a dataset."""
    if not exists(dataset):
        return []
    out = run("list", "-H", "-o", "name", "-t", "snapshot", "-d", "1", dataset)
    return out.split()


def clones_of(snapshot_name):
    """Return the datasets cloned from a snapshot."""
    out = run(
        "get", "-Hp", "-o", "value", "clones", snapshot_name, dataset=snapshot_name
    )
    value = out.strip()
    if value in ("", "-"):
        return []
    return value.split(",")


def rollback(dataset, name):
    """Roll a zvol back to one of its own snapshots (data and volsize)."""
    assert_under_root(dataset)
    full = "%s@%s" % (dataset, name)
    LOG.info(
        "rolling %(dataset)s back to %(snap)s", {"dataset": dataset, "snap": full}
    )
    run("rollback", full, dataset=full)
