"""Tests for the guest lifecycle watcher."""

import unittest
from unittest import mock

from nova.compute import power_state

from nova.virt import event as virtevent
from nova_bhyve import lifecycle

UUID = "b1288d84-9474-4210-963a-94f9453857a6"
OTHER = "638d0eea-ef85-42e1-8c12-b74570fc5d87"

RUNNING = power_state.RUNNING
SHUTDOWN = power_state.SHUTDOWN
CRASHED = power_state.CRASHED


class LifecycleWatcherTestCase(unittest.TestCase):
    """LifecycleWatcher emits a stop once it is seen twice in a row."""

    def setUp(self):
        """Build a watcher over a scripted machine."""
        self.states = {}
        self.machine = mock.Mock()
        self.machine.list_uuids.side_effect = lambda: list(self.states)
        self.machine.state.side_effect = lambda uuid: (self.states[uuid], "")
        self.emit = mock.Mock()
        self.watcher = lifecycle.LifecycleWatcher(self.machine, self.emit)

    def poll(self, **states):
        """Poll once with the given states keyed by uuid."""
        self.states = {UUID if k == "guest" else OTHER: v for k, v in states.items()}
        self.watcher.poll()

    def emitted(self):
        """Return (uuid, transition) for every emitted event."""
        return [
            (call.args[0].get_instance_uuid(), call.args[0].get_transition())
            for call in self.emit.call_args_list
        ]

    def test_stop_seen_twice_is_emitted(self):
        """A guest that stays shut down after running emits one stop."""
        for state in (RUNNING, RUNNING, SHUTDOWN, SHUTDOWN, SHUTDOWN):
            self.poll(guest=state)
        self.assertEqual([(UUID, virtevent.EVENT_LIFECYCLE_STOPPED)], self.emitted())

    def test_reboot_gap_is_not_emitted(self):
        """A single shut down poll between running polls emits nothing."""
        for state in (RUNNING, RUNNING, SHUTDOWN, RUNNING, RUNNING):
            self.poll(guest=state)
        self.emit.assert_not_called()

    def test_unconfirmed_running_is_not_emitted(self):
        """A guest seen running only once before stopping emits nothing."""
        for state in (SHUTDOWN, SHUTDOWN, RUNNING, SHUTDOWN, SHUTDOWN):
            self.poll(guest=state)
        self.emit.assert_not_called()

    def test_stopped_at_startup_is_not_emitted(self):
        """A guest already shut down when watching starts emits nothing."""
        for state in (SHUTDOWN, SHUTDOWN, SHUTDOWN):
            self.poll(guest=state)
        self.emit.assert_not_called()

    def test_crash_is_not_emitted(self):
        """A crashed guest emits nothing."""
        for state in (RUNNING, RUNNING, CRASHED, CRASHED):
            self.poll(guest=state)
        self.emit.assert_not_called()

    def test_guests_are_tracked_separately(self):
        """Only the guest that stopped emits."""
        self.poll(guest=RUNNING, other=RUNNING)
        self.poll(guest=RUNNING, other=RUNNING)
        self.poll(guest=SHUTDOWN, other=RUNNING)
        self.poll(guest=SHUTDOWN, other=SHUTDOWN)
        self.assertEqual([(UUID, virtevent.EVENT_LIFECYCLE_STOPPED)], self.emitted())

    def test_undefined_guest_is_forgotten(self):
        """A guest that disappears and returns starts over."""
        self.poll(guest=RUNNING)
        self.poll(guest=RUNNING)
        self.poll()
        self.poll(guest=SHUTDOWN)
        self.poll(guest=SHUTDOWN)
        self.emit.assert_not_called()

    def test_run_survives_a_failed_poll(self):
        """An exception in one poll does not end the loop."""
        polls = []

        def poll():
            polls.append(None)
            if len(polls) == 1:
                raise RuntimeError("boom")
            self.watcher.stopping.set()

        self.watcher.poll = poll
        with mock.patch.object(lifecycle, "POLL_INTERVAL", 0):
            self.watcher.run()
        self.assertEqual(2, len(polls))

    def test_stop_ends_the_loop(self):
        """Run returns without polling once stop is called."""
        self.watcher.poll = mock.Mock()
        self.watcher.stop()
        self.watcher.run()
        self.watcher.poll.assert_not_called()
