"""Tests for cold migration between hosts."""

import io
import os
import subprocess
import unittest
from unittest import mock

import nova.conf
from nova.compute import power_state
from oslo_utils import units

from nova import exception as nova_exception
from nova_bhyve import exception, migration
from nova_bhyve.machine import direct
from tests.test_local_disks import (
    EPH0,
    ROOT,
    SWAP,
    UUID,
    LocalDiskTestCase,
    make_bdi,
    make_instance,
)

CONF = nova.conf.CONF

DEST = "192.0.2.20"


class SendTestCase(LocalDiskTestCase):
    """The source sends its disks and files to another host."""

    def setUp(self):
        """Give the instance disks and files and fake the transport."""
        super().setUp()
        self.zfs.volumes.update(
            {ROOT: 10 * units.Gi, EPH0: units.Gi, SWAP: 512 * units.Mi}
        )
        self.bdi = make_bdi([("/dev/vdb", 1)], swap_mb=512)
        self.directory = os.path.join(self.tmp.name, UUID)
        os.makedirs(self.directory)
        for name in (direct.NVRAM_FILE, direct.SPEC_FILE):
            open(os.path.join(self.directory, name), "w").close()
        self.driver._disconnect_volumes = mock.Mock()
        self.transport = mock.Mock()
        for name in ("send_volume", "send_file", "discard"):
            mock.patch.object(migration, name, getattr(self.transport, name)).start()

    def migrate(self, dest=DEST):
        """Run migrate_disk_and_power_off towards dest."""
        flavor = mock.Mock(root_gb=10, ephemeral_gb=1)
        return self.driver.migrate_disk_and_power_off(
            None, make_instance(ephemeral_gb=1), dest, flavor, None, self.bdi
        )

    def test_sends_disks_except_swap(self):
        """Root and ephemeral zvols are sent from their revert points."""
        self.migrate()
        self.transport.send_volume.assert_has_calls(
            [
                mock.call(DEST, UUID, "root", ROOT + "@nova-resize"),
                mock.call(DEST, UUID, "eph0", EPH0 + "@nova-resize"),
            ]
        )
        self.assertEqual(2, self.transport.send_volume.call_count)

    def test_sends_only_present_instance_files(self):
        """The NVRAM is sent and the absent config drive is skipped."""
        self.migrate()
        self.transport.send_file.assert_called_once_with(
            DEST, UUID, os.path.join(self.directory, direct.NVRAM_FILE)
        )

    def test_keeps_revert_points_and_disconnects_volumes(self):
        """The source keeps its zvols and snapshots and logs out of volumes."""
        self.migrate()
        self.assertIn(ROOT + "@nova-resize", self.zfs.snapshots)
        self.assertIn(ROOT, self.zfs.volumes)
        self.driver._disconnect_volumes.assert_called_once_with(self.bdi)
        self.driver._machine.start.assert_not_called()

    def test_same_host_sends_nothing(self):
        """A resize on the same host does not touch the transport."""
        self.migrate(dest=CONF.my_ip)
        self.transport.send_volume.assert_not_called()
        self.driver._disconnect_volumes.assert_not_called()

    def test_failed_send_rolls_back(self):
        """A failed send discards the remote copy and restarts the guest."""
        self.transport.send_volume.side_effect = exception.BhyveDriverError(
            reason="broken pipe"
        )
        self.assertRaises(nova_exception.InstanceFaultRollback, self.migrate)
        self.transport.discard.assert_called_once_with(DEST, UUID)
        self.assertEqual({}, self.zfs.snapshots)
        self.driver._machine.start.assert_called_once_with(UUID)

    def test_failed_send_leaves_stopped_guest_stopped(self):
        """A guest that was shut off before the migration stays off."""
        self.driver._machine.state.return_value = (power_state.SHUTDOWN, "")
        self.transport.send_file.side_effect = OSError("no ssh")
        self.transport.discard.side_effect = OSError("no ssh")
        self.assertRaises(nova_exception.InstanceFaultRollback, self.migrate)
        self.driver._machine.start.assert_not_called()

    def test_confirm_after_move_removes_source_copy(self):
        """Confirming a move to another host deletes every local trace."""
        self.migrate()
        instance = make_instance()
        instance.host = "dest-host"
        self.driver.confirm_migration(None, None, instance, None)
        self.assertEqual({}, self.zfs.volumes)
        self.assertEqual({}, self.zfs.snapshots)
        self.assertFalse(os.path.exists(self.directory))


class ReceiveTestCase(LocalDiskTestCase):
    """The receiver lands disks and files under this host's dataset."""

    def setUp(self):
        """Fake zfs receive and point stdin at a buffer."""
        super().setUp()
        self.run = mock.patch.object(subprocess, "run").start()
        self.run.side_effect = self.fake_receive
        self.directory = os.path.join(self.tmp.name, UUID)

    def fake_receive(self, argv, **kwargs):
        """Create the received zvol with the sender's snapshot."""
        dataset = argv[-1]
        self.zfs.volumes[dataset] = units.Gi
        self.zfs.snapshots[dataset + "@nova-resize"] = units.Gi

    def stdin(self, data):
        """Feed data to the receiver on stdin."""
        stdin = mock.Mock(buffer=io.BytesIO(data))
        mock.patch("sys.stdin", stdin).start()

    def define(self):
        """Mark the instance as defined on this host."""
        os.makedirs(self.directory, exist_ok=True)
        open(os.path.join(self.directory, direct.SPEC_FILE), "w").close()

    def test_receives_root_and_drops_sender_snapshot(self):
        """The root stream lands in the instance dataset without snapshots."""
        self.assertEqual(0, migration.serve("receive %s root" % UUID))
        self.assertEqual(["zfs", "receive", "-u", ROOT], self.run.call_args[0][0])
        self.assertEqual({ROOT: units.Gi}, self.zfs.volumes)
        self.assertEqual({}, self.zfs.snapshots)

    def test_receives_local_disk(self):
        """A named disk lands next to the root zvol."""
        self.assertEqual(0, migration.serve("receive %s eph0" % UUID))
        self.assertIn(EPH0, self.zfs.volumes)

    def test_refuses_existing_dataset(self):
        """A dataset that already exists is not overwritten."""
        self.zfs.volumes[ROOT] = units.Gi
        self.assertEqual(1, migration.serve("receive %s root" % UUID))
        self.run.assert_not_called()

    def test_refuses_defined_instance(self):
        """Nothing is received for an instance already defined here."""
        self.define()
        self.assertEqual(1, migration.serve("receive %s eph0" % UUID))
        self.run.assert_not_called()

    def test_refuses_bad_arguments(self):
        """Unknown verbs, bad uuids and path-like names are refused."""
        for command in (
            "",
            "zfs destroy -r tank",
            "receive ../../etc root",
            "receive %s ../x" % UUID,
            "receive %s" % UUID,
            "file %s machine.json" % UUID,
        ):
            self.assertEqual(1, migration.serve(command), command)
        self.run.assert_not_called()

    def test_receives_nvram(self):
        """An allowed instance file is written from stdin."""
        self.stdin(b"vars")
        self.assertEqual(0, migration.serve("file %s %s" % (UUID, direct.NVRAM_FILE)))
        with open(os.path.join(self.directory, direct.NVRAM_FILE), "rb") as handle:
            self.assertEqual(b"vars", handle.read())

    def test_discard_removes_received_copy(self):
        """Discard removes the received zvols, files and directory."""
        migration.serve("receive %s root" % UUID)
        self.stdin(b"vars")
        migration.serve("file %s %s" % (UUID, direct.NVRAM_FILE))
        self.assertEqual(0, migration.serve("discard %s" % UUID))
        self.assertEqual({}, self.zfs.volumes)
        self.assertFalse(os.path.exists(self.directory))

    def test_discard_refuses_defined_instance(self):
        """Discard never touches an instance that runs here."""
        self.zfs.volumes[ROOT] = units.Gi
        self.define()
        self.assertEqual(1, migration.serve("discard %s" % UUID))
        self.assertIn(ROOT, self.zfs.volumes)


class TransportTestCase(unittest.TestCase):
    """The sender reports remote failures."""

    def test_send_file_reports_remote_error(self):
        """The receiver's stderr ends up in the raised error."""
        with (
            mock.patch.object(
                migration, "remote", return_value=["sh", "-c", "echo nope >&2; exit 1"]
            ),
            self.assertRaises(exception.BhyveDriverError) as caught,
        ):
            migration.send_file(DEST, UUID, os.devnull)
        self.assertIn("nope", str(caught.exception))

    def test_send_volume_reports_failed_send(self):
        """A zfs send failure raises even when the receiver succeeds."""
        popen = subprocess.Popen

        def fake_popen(argv, **kwargs):
            """Run a failing command in place of zfs send."""
            if argv[0] == "zfs":
                argv = ["sh", "-c", "echo no such dataset >&2; exit 1"]
            return popen(argv, **kwargs)

        with (
            mock.patch.object(migration, "remote", return_value=["cat"]),
            mock.patch.object(subprocess, "Popen", fake_popen),
            self.assertRaises(exception.BhyveDriverError) as caught,
        ):
            migration.send_volume(DEST, UUID, "root", ROOT + "@nova-resize")
        self.assertIn("no such dataset", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
