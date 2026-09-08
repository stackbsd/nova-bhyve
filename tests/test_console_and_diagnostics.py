"""Tests for the console log and diagnostics paths."""

import datetime
import os
import tempfile
import unittest
from unittest import mock

import nova.conf
from nova.compute import power_state
from oslo_utils import timeutils

from nova import exception as nova_exception
from nova_bhyve import driver as driver_mod
from nova_bhyve import machine
from nova_bhyve.machine import direct

CONF = nova.conf.CONF

UUID = "11111111-2222-3333-4444-555555555555"

# Shaped like the real thing: space-padded counter names, one tab, the value.
# Resident memory is VM-wide but bhyvectl prints it inside a vcpu block.
SAMPLE_STATS = (
    "vcpu0 stats:\n"
    "vcpu total runtime                      \t8261929024600\n"
    "number of times hlt was intercepted     \t2871466\n"
    "Resident memory                         \t236937216\n"
    "vcpu1 stats:\n"
    "vcpu total runtime                      \t1234567890\n"
    "number of times hlt was intercepted     \t99\n"
)


def make_spec(**kwargs):
    """Return a MachineSpec with test defaults, overridable per test."""
    base = dict(
        uuid=UUID,
        title="test",
        vcpus=2,
        mem_mib=1024,
        root_dev="/dev/zvol/tank/nova/instances/inst",
    )
    base.update(kwargs)
    return machine.MachineSpec(**base)


class ParseStatsTestCase(unittest.TestCase):
    """The bhyvectl output parser degrades to no data, never a wrong number."""

    def test_structures_per_vcpu_runtime_and_resident_memory(self):
        """Runtime per vcpu and resident memory come back structured."""
        stats = direct.parse_stats(SAMPLE_STATS)
        self.assertEqual(
            [{"id": 0, "time": 8261929024600}, {"id": 1, "time": 1234567890}],
            stats["cpus"],
        )
        self.assertEqual(236937216, stats["memory_resident_bytes"])

    def test_no_text_is_no_stats(self):
        """None and empty text yield no stats."""
        self.assertIsNone(direct.parse_stats(None))
        self.assertIsNone(direct.parse_stats(""))

    def test_unrecognised_text_is_no_stats_not_zeros(self):
        """Foreign text yields no stats rather than zeros."""
        self.assertIsNone(direct.parse_stats("something else entirely\n"))

    def test_a_mangled_value_is_skipped_not_guessed(self):
        """A non-numeric counter value is skipped."""
        text = "vcpu0 stats:\nvcpu total runtime\t not-a-number\n"
        self.assertIsNone(direct.parse_stats(text))


class DescribeStatsTestCase(unittest.TestCase):
    """describe() carries the parsed stats and the memory ceiling."""

    def setUp(self):
        """Persist a spec for the test instance."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        CONF.set_override("instances_path", self.tmp.name)
        self.addCleanup(CONF.clear_override, "instances_path")
        self.machine = direct.DirectMachine()
        self.machine._write_spec(make_spec())

    def test_describe_carries_parsed_stats_and_the_memory_ceiling(self):
        """Stats and mem_mib both appear in the details."""
        with mock.patch.object(direct.privsep, "vm_stats", return_value=SAMPLE_STATS):
            details = self.machine.describe(UUID)
        self.assertEqual(1024, details["mem_mib"])
        self.assertEqual(2, len(details["stats"]["cpus"]))

    def test_a_stopped_instance_has_no_stats_key(self):
        """No /dev/vmm object to read means no stats key at all."""
        with mock.patch.object(direct.privsep, "vm_stats", return_value=None):
            details = self.machine.describe(UUID)
        self.assertNotIn("stats", details)


class GetConsoleOutputTestCase(unittest.TestCase):
    """The console read stays an unprivileged tail of a bounded size."""

    def setUp(self):
        """Build a driver with a temporary console log path."""
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.driver = driver_mod.BhyveDriver(mock.Mock())
        self.instance = mock.Mock(uuid=UUID)
        self.log_path = os.path.join(self.tmp.name, "console.log")

    def _with_log(self):
        """Patch describe() to report the test console log path."""
        return mock.patch.object(
            self.driver._machine,
            "describe",
            return_value={"console_log": self.log_path},
        )

    def test_returns_the_log_as_bytes(self):
        """A short log comes back whole, as bytes."""
        with open(self.log_path, "wb") as handle:
            handle.write(b"guest said hello\n")
        with self._with_log():
            output = self.driver.get_console_output(mock.Mock(), self.instance)
        self.assertEqual(b"guest said hello\n", output)

    def test_a_long_log_is_tailed_not_returned_whole(self):
        """A log over the cap is tailed to MAX_CONSOLE_BYTES."""
        with open(self.log_path, "wb") as handle:
            handle.write(b"x" * driver_mod.MAX_CONSOLE_BYTES)
            handle.write(b"the end")
        with self._with_log():
            output = self.driver.get_console_output(mock.Mock(), self.instance)
        self.assertEqual(driver_mod.MAX_CONSOLE_BYTES, len(output))
        self.assertTrue(output.endswith(b"the end"))

    def test_defined_but_never_booted_reads_as_empty(self):
        """No log file yet reads as empty bytes."""
        with self._with_log():
            output = self.driver.get_console_output(mock.Mock(), self.instance)
        self.assertEqual(b"", output)

    def test_a_mechanism_without_a_log_says_unavailable(self):
        """A mechanism reporting no console_log raises ConsoleNotAvailable."""
        # An empty answer would be a lie about a console that was never
        # captured.
        with mock.patch.object(self.driver._machine, "describe", return_value={}):
            self.assertRaises(
                nova_exception.ConsoleNotAvailable,
                self.driver.get_console_output,
                mock.Mock(),
                self.instance,
            )


class GetInstanceDiagnosticsTestCase(unittest.TestCase):
    """The versioned diagnostics answer, built from the mechanism's stats."""

    def setUp(self):
        """Build a driver and an instance with a known launch time."""
        super().setUp()
        self.driver = driver_mod.BhyveDriver(mock.Mock())
        self.instance = mock.Mock(
            uuid=UUID, launched_at=timeutils.utcnow() - datetime.timedelta(seconds=30)
        )
        mock.patch.object(
            driver_mod.configdrive, "required_by", return_value=False
        ).start()
        self.addCleanup(mock.patch.stopall)

    def test_reports_the_parsed_stats(self):
        """CPU, memory and uptime come from the stats; disks and nics stay empty."""
        details = {
            "mem_mib": 1024,
            "stats": {
                "cpus": [
                    {"id": 0, "time": 8261929024600},
                    {"id": 1, "time": 1234567890},
                ],
                "memory_resident_bytes": 236937216,
            },
        }
        with (
            mock.patch.object(self.driver._machine, "describe", return_value=details),
            mock.patch.object(
                self.driver._machine,
                "state",
                return_value=(power_state.RUNNING, "running"),
            ),
        ):
            diags = self.driver.get_instance_diagnostics(self.instance)

        self.assertEqual("running", diags.state)
        self.assertEqual("bhyve", diags.hypervisor)
        self.assertEqual("freebsd", diags.hypervisor_os)
        self.assertEqual(2, diags.num_cpus)
        self.assertEqual(8261929024600, diags.cpu_details[0].time)
        self.assertEqual(1024, diags.memory_details.maximum)
        self.assertEqual(225, diags.memory_details.used)
        self.assertGreaterEqual(diags.uptime, 30)
        # bhyve has no per-disk or per-nic counters; empty is the truth.
        self.assertEqual(0, diags.num_disks)
        self.assertEqual(0, diags.num_nics)
        # 'driver' is an enum of in-tree drivers; it must stay unset rather
        # than claim to be one of them.
        self.assertFalse(diags.obj_attr_is_set("driver"))

    def test_a_mechanism_without_stats_stays_unimplemented(self):
        """A mechanism reporting no stats raises NotImplementedError."""
        with mock.patch.object(
            self.driver._machine, "describe", return_value={"tap_names": []}
        ):
            self.assertRaises(
                NotImplementedError, self.driver.get_instance_diagnostics, self.instance
            )
