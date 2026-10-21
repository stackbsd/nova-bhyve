"""Cold migration transport over ssh."""

import os
import re
import subprocess
import sys

import nova.conf

from nova.virt.bhyve import privsep
from nova_bhyve import exception, machine
from nova_bhyve.machine import direct
from nova_bhyve.storage import zfs

CONF = nova.conf.CONF

SSH = ("ssh", "-o", "BatchMode=yes")

ROOT_DISK = "root"
DISK_NAME_RE = re.compile(r"^[a-z0-9]+$")

INSTANCE_FILES = (direct.NVRAM_FILE, machine.CONFIG_DRIVE_NAME)


def remote(host, *words):
    """Return the ssh command line running a receiver verb on a host."""
    return [*SSH, host, *words]


def check_result(proc, err, what, host):
    """Raise if a transfer process exited non-zero."""
    if proc.returncode:
        raise exception.BhyveDriverError(
            reason="%(what)s to %(host)s failed: %(err)s"
            % {"what": what, "host": host, "err": err.decode(errors="replace").strip()}
        )


def send_volume(host, uuid, disk, snapshot_name):
    """Stream a zvol snapshot into the receiver on another host."""
    with subprocess.Popen(
        ["zfs", "send", "-c", snapshot_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as send:
        recv = subprocess.Popen(
            remote(host, "receive", uuid, disk),
            stdin=send.stdout,
            stderr=subprocess.PIPE,
        )
        # lets zfs send see EPIPE if ssh exits early
        send.stdout.close()
        _out, recv_err = recv.communicate()
        send_err = send.stderr.read()
    check_result(send, send_err, "sending %s" % snapshot_name, host)
    check_result(recv, recv_err, "receiving %s" % snapshot_name, host)


def send_file(host, uuid, path):
    """Copy an instance file into the receiver on another host."""
    with open(path, "rb") as handle:
        proc = subprocess.run(
            remote(host, "file", uuid, os.path.basename(path)),
            stdin=handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    check_result(proc, proc.stderr, "copying %s" % path, host)


def discard(host, uuid):
    """Remove whatever a failed transfer left on another host."""
    proc = subprocess.run(
        remote(host, "discard", uuid), stderr=subprocess.PIPE, check=False
    )
    check_result(proc, proc.stderr, "discarding %s" % uuid, host)


def disk_dataset(uuid, disk):
    """Return the local dataset a received disk lands in."""
    if disk == ROOT_DISK:
        return zfs.instance_dataset(uuid)
    if not DISK_NAME_RE.match(disk):
        raise ValueError("refusing disk name %r" % disk)
    return zfs.local_disk_dataset(uuid, disk)


def assert_not_defined(uuid):
    """Raise if an instance is already defined on this host."""
    if os.path.exists(os.path.join(direct.uuid_dir(uuid), direct.SPEC_FILE)):
        raise ValueError("%s is already defined on this host" % uuid)


def receive_volume(uuid, disk):
    """Receive a zvol stream from stdin."""
    assert_not_defined(uuid)
    dataset = disk_dataset(uuid, disk)
    zfs.assert_under_root(dataset)
    if zfs.exists(dataset):
        raise ValueError("%s already exists" % dataset)
    subprocess.run(["zfs", "receive", "-u", dataset], stdin=sys.stdin, check=True)
    # the stream carries the sender's revert point which is useless here
    for snapshot_name in zfs.snapshots_of(dataset):
        zfs.destroy_deferred(snapshot_name)


def receive_file(uuid, name):
    """Receive an instance file from stdin."""
    assert_not_defined(uuid)
    if name not in INSTANCE_FILES:
        raise ValueError("refusing file name %r" % name)
    directory = direct.uuid_dir(uuid)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path + ".tmp", "wb") as handle:
        while chunk := sys.stdin.buffer.read(1024 * 1024):
            handle.write(chunk)
    os.rename(path + ".tmp", path)


def discard_instance(uuid):
    """Remove the received disks and files of an undefined instance."""
    assert_not_defined(uuid)
    for dataset in (zfs.instance_dataset(uuid), *zfs.local_disks(uuid)):
        zfs.destroy_volume_and_snapshots(dataset)
    directory = direct.uuid_dir(uuid)
    for name in INSTANCE_FILES:
        path = os.path.join(directory, name)
        if os.path.exists(path):
            os.unlink(path)
    if os.path.isdir(directory) and not os.listdir(directory):
        os.rmdir(directory)


VERBS = {
    "receive": (receive_volume, 2),
    "file": (receive_file, 2),
    "discard": (discard_instance, 1),
}


def serve(command):
    """Run one receiver verb and return the exit status."""
    words = command.split()
    try:
        if not words or words[0] not in VERBS:
            raise ValueError("expected one of %s" % ", ".join(sorted(VERBS)))
        func, nargs = VERBS[words[0]]
        args = words[1:]
        if len(args) != nargs:
            raise ValueError("%s takes %d arguments" % (words[0], nargs))
        privsep.check_name(args[0])
        func(*args)
    except (ValueError, OSError, subprocess.CalledProcessError) as err:
        sys.stderr.write("nova-bhyve-receive: %s\n" % err)
        return 1
    except exception.BhyveDriverError as err:
        sys.stderr.write("nova-bhyve-receive: %s\n" % err.format_message())
        return 1
    return 0


def main():
    """Serve the receiver verb named by SSH_ORIGINAL_COMMAND."""
    CONF(sys.argv[1:], project="nova")
    return serve(os.environ.get("SSH_ORIGINAL_COMMAND", ""))


if __name__ == "__main__":
    sys.exit(main())
