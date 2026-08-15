"""Tests for lib/managers/sensor_manager.py.

The isolation tests here are CLAUDE.md's rule that one misbehaving sensor or VM
must never block the rest of the poll. Retry backoff is patched out: the real
0.5s ramp governs recovery on hardware, not correctness, and paying it three
times per failing sensor would dominate the suite.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from lib.hardware.gpu import GPUSensor
from lib.hardware.ipmi import IPMIError, IPMISensor
from lib.hardware.sensor import Sensor
from lib.managers.sensor_manager import SensorManager
from lib.models.vm import VMConfig
from lib.state import State

_LOGGER = "lib.managers.sensor_manager"

# Long enough that no test re-scans by accident. Tests that are about the
# re-scan ask for one explicitly with rediscover_interval=0.
_NEVER = 1_000_000.0


def _vm(name="vm1", socket="/run/qemu/vm1-ga.sock") -> VMConfig:
    return VMConfig.from_dict(
        {
            "name": {"vm": name},
            "qga": {"socket": socket},
            "gpu": {"type": "nvidia"},
            "limits": {"max_temperature": 85},
        },
    )


class FakeVMManager:
    def __init__(self, **vms: VMConfig):
        self._vms = vms

    def __iter__(self):
        return iter(self._vms.items())


class FakeIPMI:
    """A BMC whose sensor list and health can change between calls.

    `names` and `error` are public and mutable because that is the scenario
    under test: a BMC that reports a short SDR while it is still initialising
    and fills it in later, or one that fails a scan and recovers.
    """

    def __init__(self, *names: str, error=None):
        self.names = list(names)
        self.error = error
        self.calls = 0
        self.cycles = 0

    def temperature_sensor_names(self) -> list[str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.names)

    def temperature_readings(self) -> dict[str, float | None]:
        # Discovery builds real IPMISensors against this fake, and they read
        # through here. Every listed sensor reports the same benign value: what
        # these tests are about is which sensors exist, not what they say.
        if self.error is not None:
            raise self.error
        return {name: 22.0 for name in self.names}

    def begin_cycle(self) -> None:
        self.cycles += 1


class FakeSensor(Sensor):
    """Healthy or broken for a whole poll, which is how real sensors behave.

    Tests flip `.value` and `.error` between polls. Modelling it per-read
    instead would let the manager's own retry silently recover inside a single
    poll, which is a different behaviour entirely.
    """

    def __init__(self, value=42.0, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def read(self) -> float:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.value


class ScriptedSensor(Sensor):
    """One scripted outcome per read(), for reasoning about retry attempts."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls = 0

    def read(self) -> float:
        self.calls += 1
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SensorManagerTestCase(unittest.TestCase):
    def setUp(self):
        # The backoff between retries is not what any assertion here is about.
        patcher = patch("lib.utils.retry.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        self.state = State()

    def manager(
        self, vm_manager=None, ipmi=None, rediscover_interval=_NEVER, **sensors,
    ) -> SensorManager:
        manager = SensorManager(
            vm_manager or FakeVMManager(),
            ipmi or FakeIPMI(),
            rediscover_interval=rediscover_interval,
        )
        if sensors:
            manager._sensors = dict(sensors)
        return manager


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
class DiscoveryTests(SensorManagerTestCase):
    def test_each_vm_contributes_a_gpu_sensor(self):
        manager = self.manager(vm_manager=FakeVMManager(vm1=_vm("vm1"), vm2=_vm("vm2")))
        manager.discover()
        self.assertEqual(set(manager._sensors), {"vm1 GPU", "vm2 GPU"})

    def test_gpu_sensors_are_gpu_sensors(self):
        manager = self.manager(vm_manager=FakeVMManager(vm1=_vm()))
        manager.discover()
        self.assertIsInstance(manager._sensors["vm1 GPU"], GPUSensor)

    def test_every_bmc_temperature_sensor_is_included(self):
        manager = self.manager(ipmi=FakeIPMI("Inlet Temp", "Exhaust Temp"))
        manager.discover()
        self.assertEqual(set(manager._sensors), {"Inlet Temp", "Exhaust Temp"})

    def test_bmc_sensors_are_ipmi_sensors(self):
        manager = self.manager(ipmi=FakeIPMI("Inlet Temp"))
        manager.discover()
        self.assertIsInstance(manager._sensors["Inlet Temp"], IPMISensor)

    def test_gpu_and_bmc_sensors_coexist(self):
        manager = self.manager(
            vm_manager=FakeVMManager(vm1=_vm()), ipmi=FakeIPMI("Inlet Temp", "Temp #2"),
        )
        manager.discover()
        self.assertEqual(set(manager._sensors), {"vm1 GPU", "Inlet Temp", "Temp #2"})

    def test_a_host_with_no_vms_still_discovers_bmc_sensors(self):
        # Not every deployment has a GPU guest; the daemon must still cool.
        manager = self.manager(ipmi=FakeIPMI("Inlet Temp"))
        manager.discover()
        self.assertEqual(set(manager._sensors), {"Inlet Temp"})

    def test_discovering_twice_replaces_rather_than_accumulates(self):
        # discover() runs again on every reload; merging would strand sensors
        # for VMs that were removed from configuration.
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(vm_manager=FakeVMManager(vm1=_vm()), ipmi=ipmi)
        manager.discover()
        manager._vm_manager = FakeVMManager(vm2=_vm("vm2"))
        manager.discover()
        self.assertEqual(set(manager._sensors), {"vm2 GPU", "Inlet Temp"})

    def test_discovery_reads_no_sensors(self):
        # Startup must not block on a guest agent that is not up yet.
        manager = self.manager(vm_manager=FakeVMManager(vm1=_vm()), ipmi=FakeIPMI("Temp"))
        manager.discover()
        self.assertEqual(self.state.temperatures, {})


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
class PollingTests(SensorManagerTestCase):
    def test_a_reading_reaches_state(self):
        manager = self.manager(**{"Inlet Temp": FakeSensor(22.0)})
        manager.poll(self.state)
        self.assertEqual(self.state.temperatures["Inlet Temp"].value, 22.0)

    def test_every_sensor_is_polled(self):
        manager = self.manager(
            **{"Inlet Temp": FakeSensor(22.0), "vm1 GPU": FakeSensor(78.0)},
        )
        manager.poll(self.state)
        self.assertEqual(set(self.state.temperatures), {"Inlet Temp", "vm1 GPU"})

    def test_polling_with_no_sensors_is_harmless(self):
        self.manager().poll(self.state)
        self.assertEqual(self.state.temperatures, {})

    def test_a_successful_poll_raises_no_alarm(self):
        manager = self.manager(**{"Inlet Temp": FakeSensor(22.0)})
        manager.poll(self.state)
        self.assertEqual(self.state.alarms, set())

    def test_readings_are_refreshed_each_poll(self):
        sensor = FakeSensor(22.0)
        manager = self.manager(**{"Inlet Temp": sensor})
        manager.poll(self.state)
        sensor.value = 23.0
        manager.poll(self.state)
        self.assertEqual(self.state.temperatures["Inlet Temp"].value, 23.0)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------
class IsolationTests(SensorManagerTestCase):
    """CLAUDE.md: one sensor or VM misbehaving must never block the rest of the
    poll loop or delay a critical-temp response elsewhere."""

    def test_a_failing_sensor_does_not_stop_the_others(self):
        # The broken one is first, so a bare raise would hide both the others.
        manager = self.manager(
            **{
                "vm1 GPU": FakeSensor(error=RuntimeError("guest agent gone")),
                "Inlet Temp": FakeSensor(22.0),
                "Exhaust Temp": FakeSensor(34.0),
            },
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual(set(self.state.temperatures), {"Inlet Temp", "Exhaust Temp"})

    def test_a_critical_reading_still_lands_behind_a_broken_sensor(self):
        # The reason the rule exists: a dead VM must not hide a cooking CPU.
        manager = self.manager(
            **{
                "vm1 GPU": FakeSensor(error=RuntimeError("guest agent gone")),
                "Temp": FakeSensor(97.0),
            },
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual(self.state.temperatures["Temp"].value, 97.0)

    def test_a_failing_sensor_raises_a_sensor_failure_alarm(self):
        manager = self.manager(**{"vm1 GPU": FakeSensor(error=RuntimeError("boom"))})
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertIn("sensor_failure:vm1 GPU", self.state.alarms)

    def test_a_failing_sensor_has_its_stale_reading_cleared(self):
        # A stale temperature is worse than none: Policy would keep cooling to
        # a number that stopped being true.
        sensor = FakeSensor(78.0)
        manager = self.manager(**{"vm1 GPU": sensor})
        manager.poll(self.state)
        sensor.error = RuntimeError("boom")
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertNotIn("vm1 GPU", self.state.temperatures)

    def test_losing_every_sensor_leaves_state_empty(self):
        # Policy turns this into EMERGENCY and full fans, which is the point.
        manager = self.manager(
            **{
                "vm1 GPU": FakeSensor(error=RuntimeError("boom")),
                "Inlet Temp": FakeSensor(error=IPMIError("BMC unreachable")),
            },
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual(self.state.temperatures, {})

    def test_any_exception_type_is_contained(self):
        for error in (
            RuntimeError("boom"),
            IPMIError("BMC unreachable"),
            OSError("socket gone"),
            ValueError("unparsable"),
        ):
            with self.subTest(error=type(error).__name__):
                state = State()
                manager = self.manager(
                    **{"bad": FakeSensor(error=error), "good": FakeSensor(22.0)},
                )
                with self.assertLogs(_LOGGER, level="WARNING"):
                    manager.poll(state)
                self.assertEqual(state.temperatures["good"].value, 22.0)


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------
class RetryTests(SensorManagerTestCase):
    def test_a_failing_read_is_retried_three_times(self):
        sensor = FakeSensor(error=RuntimeError("boom"))
        manager = self.manager(**{"vm1 GPU": sensor})
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual(sensor.calls, 3)

    def test_a_read_that_recovers_within_its_retries_succeeds(self):
        # A transient blip must not be reported as a lost sensor.
        sensor = ScriptedSensor(RuntimeError("blip"), RuntimeError("blip"), 78.0)
        manager = self.manager(**{"vm1 GPU": sensor})
        manager.poll(self.state)
        self.assertEqual(self.state.temperatures["vm1 GPU"].value, 78.0)
        self.assertEqual(self.state.alarms, set())

    def test_backoff_waits_between_attempts_but_not_after_the_last(self):
        manager = self.manager(**{"vm1 GPU": FakeSensor(error=RuntimeError("boom"))})
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5, 1.0])


# ---------------------------------------------------------------------------
# Failure and recovery reporting
# ---------------------------------------------------------------------------
class ReportingTests(SensorManagerTestCase):
    """Edge-triggered on purpose: a sensor that has been dead for a week must
    not write a warning every poll interval for a week."""

    def test_a_loss_is_logged(self):
        manager = self.manager(**{"vm1 GPU": FakeSensor(error=RuntimeError("boom"))})
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            manager.poll(self.state)
        self.assertTrue([line for line in logs.output if "lost" in line])

    def test_a_loss_is_logged_only_once(self):
        manager = self.manager(**{"vm1 GPU": FakeSensor(error=RuntimeError("boom"))})
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            for _ in range(5):
                manager.poll(self.state)
        self.assertEqual(len([line for line in logs.output if "lost" in line]), 1)

    def test_the_alarm_is_re_raised_every_poll_even_though_the_log_is_not(self):
        # The log is for humans; the alarm is state, and notifications read it.
        manager = self.manager(**{"vm1 GPU": FakeSensor(error=RuntimeError("boom"))})
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
            self.state.clear_alarm("sensor_failure:vm1 GPU")
            manager.poll(self.state)
        self.assertIn("sensor_failure:vm1 GPU", self.state.alarms)

    def test_a_recovery_is_logged(self):
        sensor = FakeSensor(error=RuntimeError("boom"))
        manager = self.manager(**{"vm1 GPU": sensor})
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            manager.poll(self.state)
            sensor.error, sensor.value = None, 78.0
            manager.poll(self.state)
        self.assertTrue([line for line in logs.output if "recovered" in line])

    def test_a_recovery_clears_the_alarm(self):
        sensor = FakeSensor(error=RuntimeError("boom"))
        manager = self.manager(**{"vm1 GPU": sensor})
        with self.assertLogs(_LOGGER, level="INFO"):
            manager.poll(self.state)
            sensor.error, sensor.value = None, 78.0
            manager.poll(self.state)
        self.assertNotIn("sensor_failure:vm1 GPU", self.state.alarms)

    def test_a_second_loss_is_logged_again_after_a_recovery(self):
        # Re-arming matters: an intermittent sensor should keep being reported.
        sensor = FakeSensor(error=RuntimeError("boom"))
        manager = self.manager(**{"vm1 GPU": sensor})
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            manager.poll(self.state)
            sensor.error, sensor.value = None, 78.0
            manager.poll(self.state)
            sensor.error = RuntimeError("boom again")
            manager.poll(self.state)
        self.assertEqual(len([line for line in logs.output if "lost" in line]), 2)

    def test_a_healthy_sensor_logs_no_warning(self):
        manager = self.manager(**{"Inlet Temp": FakeSensor(22.0)})
        with self.assertNoLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)

    def test_one_sensor_failing_does_not_report_another_as_lost(self):
        manager = self.manager(
            **{"vm1 GPU": FakeSensor(error=RuntimeError("boom")), "Inlet Temp": FakeSensor(22.0)},
        )
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            manager.poll(self.state)
        self.assertFalse([line for line in logs.output if "Inlet Temp" in line])


# ---------------------------------------------------------------------------
# Cycle boundaries
# ---------------------------------------------------------------------------
class CycleBoundaryTests(SensorManagerTestCase):
    """Each poll opens a cycle, so the BMC is queried once instead of once per
    sensor and every reading in a cycle comes from the same sample."""

    def test_each_poll_begins_a_cycle(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, **{"Inlet Temp": FakeSensor(22.0)})
        manager.poll(self.state)
        self.assertEqual(ipmi.cycles, 1)

    def test_every_poll_begins_its_own_cycle(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, **{"Inlet Temp": FakeSensor(22.0)})
        for _ in range(3):
            manager.poll(self.state)
        self.assertEqual(ipmi.cycles, 3)

    def test_the_cycle_begins_before_any_sensor_is_read(self):
        # Reading before invalidating would serve the previous cycle's cached
        # table, leaving every reading exactly one poll out of date forever.
        ipmi = FakeIPMI("Inlet Temp")
        seen = []

        class RecordingSensor(Sensor):
            def read(self) -> float:
                seen.append(ipmi.cycles)
                return 22.0

        manager = self.manager(ipmi=ipmi, **{"Inlet Temp": RecordingSensor()})
        manager.poll(self.state)
        self.assertEqual(seen, [1])

    def test_a_poll_with_no_sensors_still_begins_a_cycle(self):
        ipmi = FakeIPMI()
        self.manager(ipmi=ipmi).poll(self.state)
        self.assertEqual(ipmi.cycles, 1)


# ---------------------------------------------------------------------------
# Re-discovery
# ---------------------------------------------------------------------------
class RediscoveryTests(SensorManagerTestCase):
    """The sensor set is not fixed at startup.

    A BMC that is still initialising -- after an AC power loss, say -- reports a
    short SDR. A set discovered once and never revisited leaves the daemon
    driving a fraction of its sensors until someone restarts it, and nothing in
    the log says so: the failed-sensor warning only fires for a sensor that was
    discovered and then broke, never for one that was never discovered.
    """

    def test_a_sensor_that_appears_later_is_picked_up(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.names.append("Exhaust Temp")
        manager.poll(self.state)
        self.assertIn("Exhaust Temp", manager._sensors)

    def test_a_sensor_that_appears_later_is_polled_in_the_same_cycle(self):
        # Picking it up but waiting a further interval to read it would be a
        # second, quieter version of the same bug.
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.names.append("Exhaust Temp")
        manager._sensors["Inlet Temp"] = FakeSensor(22.0)
        manager.poll(self.state)
        self.assertIn("Exhaust Temp", self.state.temperatures)

    def test_a_sensor_that_vanishes_is_dropped(self):
        ipmi = FakeIPMI("Inlet Temp", "Exhaust Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.names.remove("Exhaust Temp")
        manager.poll(self.state)
        self.assertNotIn("Exhaust Temp", manager._sensors)

    def test_an_unchanged_sensor_keeps_its_object(self):
        # Rebuilding every sensor on every scan would discard whatever state a
        # sensor implementation holds, for no reason.
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        original = manager._sensors["Inlet Temp"]
        manager.poll(self.state)
        self.assertIs(manager._sensors["Inlet Temp"], original)

    def test_a_dropped_sensor_is_pruned_from_failure_tracking(self):
        # Otherwise the set grows without bound across re-scans, and a name
        # that came back would be reported as recovered without ever being lost.
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        manager._sensors["Inlet Temp"] = FakeSensor(error=IPMIError("no reading"))
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertIn("Inlet Temp", manager._failed_sensors)
        ipmi.names.clear()
        manager.poll(self.state)
        self.assertNotIn("Inlet Temp", manager._failed_sensors)

    def test_a_dropped_sensor_has_its_reading_cleared(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        manager._sensors["Inlet Temp"] = FakeSensor(22.0)
        manager.poll(self.state)
        ipmi.names.clear()
        manager.poll(self.state)
        self.assertNotIn("Inlet Temp", self.state.temperatures)

    def test_the_interval_is_respected(self):
        # A re-scan is an extra ipmitool invocation; doing it every poll would
        # undo the point of caching the table.
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi)
        manager.discover()
        before = ipmi.calls
        for _ in range(5):
            manager.poll(self.state)
        self.assertEqual(ipmi.calls, before)

    def test_vm_sensors_survive_a_rescan(self):
        # Re-scanning the BMC must not disturb the GPU sensors, which come from
        # configuration rather than from the SDR.
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(
            vm_manager=FakeVMManager(vm1=_vm()), ipmi=ipmi, rediscover_interval=0,
        )
        manager.discover()
        manager.poll(self.state)
        self.assertIn("vm1 GPU", manager._sensors)


# ---------------------------------------------------------------------------
# Re-discovery failure
# ---------------------------------------------------------------------------
class RediscoveryFailureTests(SensorManagerTestCase):
    """A scan that fails must leave the previous set alone.

    Policy reads an empty temperature set as "no data" and answers with
    EMERGENCY and 100% fans. If a transient ipmitool failure could empty the
    sensor set, this fix would have turned a blip into a chassis running every
    fan flat out.
    """

    def test_a_failed_rescan_keeps_the_previous_sensors(self):
        ipmi = FakeIPMI("Inlet Temp", "Exhaust Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.error = IPMIError("BMC unreachable")
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual(set(manager._sensors), {"Inlet Temp", "Exhaust Temp"})

    def test_a_failed_rescan_does_not_empty_state(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        manager._sensors["Inlet Temp"] = FakeSensor(22.0)
        ipmi.error = IPMIError("BMC unreachable")
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual(self.state.temperatures["Inlet Temp"].value, 22.0)

    def test_a_failed_rescan_is_logged(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.error = IPMIError("BMC unreachable")
        with self.assertLogs(_LOGGER, level="WARNING") as logs:
            manager.poll(self.state)
        self.assertTrue([line for line in logs.output if "BMC unreachable" in line])

    def test_a_rescan_recovers_after_a_failure(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.error = IPMIError("BMC unreachable")
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        ipmi.error = None
        ipmi.names.append("Exhaust Temp")
        manager.poll(self.state)
        self.assertIn("Exhaust Temp", manager._sensors)

    def test_a_failed_rescan_does_not_stop_the_poll(self):
        # The whole poll must still run: a scan is housekeeping, and a cooking
        # CPU cannot wait for the BMC's sensor list to come back.
        ipmi = FakeIPMI("Temp", error=IPMIError("BMC unreachable"))
        manager = self.manager(
            ipmi=ipmi, rediscover_interval=0, **{"Temp": FakeSensor(97.0)},
        )
        with self.assertLogs(_LOGGER, level="WARNING"):
            manager.poll(self.state)
        self.assertEqual(self.state.temperatures["Temp"].value, 97.0)


# ---------------------------------------------------------------------------
# Discovery reporting
# ---------------------------------------------------------------------------
class DiscoveryReportingTests(SensorManagerTestCase):
    """The set the daemon is driving has to be visible in the journal.

    A sensor that is never discovered produces no other log line at all, which
    is why an incomplete sensor set went unnoticed on real hardware.
    """

    def test_the_discovered_set_is_logged(self):
        manager = self.manager(ipmi=FakeIPMI("Inlet Temp", "Exhaust Temp"))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            manager.discover()
        self.assertTrue([line for line in logs.output if "Inlet Temp" in line])

    def test_gpu_sensors_are_named_in_the_log_too(self):
        manager = self.manager(
            vm_manager=FakeVMManager(vm1=_vm()), ipmi=FakeIPMI("Inlet Temp"),
        )
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            manager.discover()
        self.assertTrue([line for line in logs.output if "vm1 GPU" in line])

    def test_discovering_nothing_is_logged(self):
        # The most important case to see in a journal, and the quietest.
        manager = self.manager(ipmi=FakeIPMI())
        with self.assertLogs(_LOGGER, level="INFO"):
            manager.discover()

    def test_an_added_sensor_is_logged(self):
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.names.append("Exhaust Temp")
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            manager.poll(self.state)
        self.assertTrue([line for line in logs.output if "Exhaust Temp" in line])

    def test_a_removed_sensor_is_logged(self):
        ipmi = FakeIPMI("Inlet Temp", "Exhaust Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        ipmi.names.remove("Exhaust Temp")
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            manager.poll(self.state)
        self.assertTrue([line for line in logs.output if "Exhaust Temp" in line])

    def test_an_unchanged_rescan_is_quiet(self):
        # Re-logging an identical set every interval would bury the scan that
        # actually changed something.
        ipmi = FakeIPMI("Inlet Temp")
        manager = self.manager(ipmi=ipmi, rediscover_interval=0)
        manager.discover()
        with self.assertNoLogs(_LOGGER, level="INFO"):
            manager.poll(self.state)


if __name__ == "__main__":
    unittest.main()
