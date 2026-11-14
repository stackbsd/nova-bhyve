"""Guest lifecycle watcher."""

import threading

from nova.compute import power_state
from oslo_log import log as logging

from nova.virt import event as virtevent

LOG = logging.getLogger(__name__)

POLL_INTERVAL = 5


class LifecycleWatcher:
    """Poller that emits an event when a running guest stops."""

    def __init__(self, machine, emit_event):
        """Record the machine layer and the event callback."""
        self.machine = machine
        self.emit_event = emit_event
        self.observed = {}
        self.confirmed = {}
        self.stopping = threading.Event()

    def start(self):
        """Start polling in a background thread."""
        self.stopping.clear()
        thread = threading.Thread(target=self.run, name="bhyve-lifecycle", daemon=True)
        thread.start()

    def stop(self):
        """Stop polling."""
        self.stopping.set()

    def run(self):
        """Poll every guest until stopped."""
        while not self.stopping.wait(POLL_INTERVAL):
            try:
                self.poll()
            except Exception:
                LOG.exception("lifecycle poll failed")

    def poll(self):
        """Confirm states seen twice in a row and emit stops."""
        states = {uuid: self.machine.state(uuid) for uuid in self.machine.list_uuids()}
        observed = {uuid: state for uuid, (state, _reason) in states.items()}
        confirmed = {}
        for uuid, (state, reason) in states.items():
            previous = self.confirmed.get(uuid)
            if self.observed.get(uuid) != state:
                if previous is not None:
                    confirmed[uuid] = previous
                continue
            confirmed[uuid] = state
            if previous == power_state.RUNNING and state == power_state.SHUTDOWN:
                LOG.info("%s stopped", uuid)
                self.emit_event(
                    virtevent.LifecycleEvent(uuid, virtevent.EVENT_LIFECYCLE_STOPPED)
                )
            if previous == power_state.RUNNING and state == power_state.CRASHED:
                LOG.warning(
                    "%(uuid)s crashed: %(reason)s", {"uuid": uuid, "reason": reason}
                )
        self.observed = observed
        self.confirmed = confirmed
