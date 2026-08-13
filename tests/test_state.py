"""Tests for lib/state.py.

State is deliberately dumb: plain setters, no evaluation. Policy is the only
thing allowed to draw conclusions from it. Several tests below exist to keep it
that way, because the emergency latch depends on State remembering exactly what
it was told and nothing more.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from lib.state import HardwareCommandResult, OperatingMode, State, TemperatureReading

_NOW = 1_700_000_000.0


class StateTestCase(unittest.TestCase):
    def setUp(self):
        self.clock = _NOW
        patcher = patch("lib.state.time.time", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state = State()


class DefaultsTests(StateTestCase):
    def test_a_new_state_has_no_readings(self):
        self.assertEqual(self.state.temperatures, {})

    def test_a_new_state_has_no_alarms(self):
        self.assertEqual(self.state.alarms, set())

    def test_a_new_state_starts_in_starting(self):
        self.assertIs(self.state.mode, OperatingMode.STARTING)

    def test_a_new_state_has_not_requested_a_fan_speed(self):
        # None, not 0.0 -- "nothing asked for yet" is what tells Policy to skip
        # hysteresis on the very first decision.
        self.assertIsNone(self.state.requested_fan_speed)

    def test_a_new_state_has_no_command_result(self):
        self.assertIsNone(self.state.last_command_result)

    def test_two_states_do_not_share_containers(self):
        other = State()
        self.state.update_temperature("CPU", 50.0)
        self.state.set_alarm("boom")
        self.assertEqual(other.temperatures, {})
        self.assertEqual(other.alarms, set())


class TemperatureTests(StateTestCase):
    def test_a_reading_is_stored_by_name(self):
        self.state.update_temperature("Exhaust Temp", 42.5)
        self.assertEqual(self.state.temperatures["Exhaust Temp"].value, 42.5)

    def test_a_reading_is_timestamped(self):
        self.state.update_temperature("Exhaust Temp", 42.5)
        self.assertEqual(self.state.temperatures["Exhaust Temp"].timestamp, _NOW)

    def test_updating_replaces_the_value_and_the_timestamp(self):
        self.state.update_temperature("CPU", 40.0)
        self.clock += 5.0
        self.state.update_temperature("CPU", 41.0)
        reading = self.state.temperatures["CPU"]
        self.assertEqual(reading.value, 41.0)
        self.assertEqual(reading.timestamp, _NOW + 5.0)
        self.assertEqual(len(self.state.temperatures), 1)

    def test_several_sensors_coexist(self):
        self.state.update_temperature("CPU", 40.0)
        self.state.update_temperature("vm1 GPU", 78.0)
        self.assertEqual(set(self.state.temperatures), {"CPU", "vm1 GPU"})

    def test_clearing_removes_the_reading(self):
        self.state.update_temperature("CPU", 40.0)
        self.state.clear_temperature("CPU")
        self.assertNotIn("CPU", self.state.temperatures)

    def test_clearing_an_absent_sensor_is_harmless(self):
        # SensorManager clears on every failed read, including the first.
        self.state.clear_temperature("never seen")
        self.assertEqual(self.state.temperatures, {})

    def test_clearing_leaves_other_sensors_alone(self):
        self.state.update_temperature("CPU", 40.0)
        self.state.update_temperature("vm1 GPU", 78.0)
        self.state.clear_temperature("CPU")
        self.assertEqual(set(self.state.temperatures), {"vm1 GPU"})

    def test_a_negative_reading_is_stored_as_given(self):
        # State does not judge; a bad sensor is Policy's and SensorManager's
        # problem, not something to silently clamp here.
        self.state.update_temperature("Inlet", -50.0)
        self.assertEqual(self.state.temperatures["Inlet"].value, -50.0)


class AlarmTests(StateTestCase):
    def test_an_alarm_is_recorded(self):
        self.state.set_alarm("over_temperature")
        self.assertIn("over_temperature", self.state.alarms)

    def test_setting_the_same_alarm_twice_is_idempotent(self):
        # Policy re-sets over_temperature on every emergency cycle.
        self.state.set_alarm("over_temperature")
        self.state.set_alarm("over_temperature")
        self.assertEqual(self.state.alarms, {"over_temperature"})

    def test_an_alarm_can_be_cleared(self):
        self.state.set_alarm("over_temperature")
        self.state.clear_alarm("over_temperature")
        self.assertNotIn("over_temperature", self.state.alarms)

    def test_clearing_an_alarm_that_was_never_set_is_harmless(self):
        # Policy clears over_temperature on every non-emergency cycle, which is
        # usually the very first one.
        self.state.clear_alarm("over_temperature")
        self.assertEqual(self.state.alarms, set())

    def test_clearing_one_alarm_leaves_the_others(self):
        self.state.set_alarm("over_temperature")
        self.state.set_alarm("sensor_failure:vm1 GPU")
        self.state.clear_alarm("over_temperature")
        self.assertEqual(self.state.alarms, {"sensor_failure:vm1 GPU"})


class ModeAndSpeedTests(StateTestCase):
    def test_the_mode_can_be_set(self):
        for mode in OperatingMode:
            with self.subTest(mode=mode):
                self.state.set_mode(mode)
                self.assertIs(self.state.mode, mode)

    def test_the_requested_speed_can_be_set(self):
        self.state.set_requested_fan_speed(62.5)
        self.assertEqual(self.state.requested_fan_speed, 62.5)

    def test_the_requested_speed_can_be_set_to_zero(self):
        # 0.0 must remain distinguishable from "not yet requested".
        self.state.set_requested_fan_speed(0.0)
        self.assertEqual(self.state.requested_fan_speed, 0.0)
        self.assertIsNotNone(self.state.requested_fan_speed)


class CommandResultTests(StateTestCase):
    def test_a_success_is_recorded_and_timestamped(self):
        self.state.set_last_command_result(success=True, detail="set to 70%")
        result = self.state.last_command_result
        self.assertTrue(result.success)
        self.assertEqual(result.detail, "set to 70%")
        self.assertEqual(result.timestamp, _NOW)

    def test_a_failure_is_recorded(self):
        self.state.set_last_command_result(success=False, detail="ipmitool exited 1")
        self.assertFalse(self.state.last_command_result.success)

    def test_the_detail_defaults_to_empty(self):
        self.state.set_last_command_result(success=True)
        self.assertEqual(self.state.last_command_result.detail, "")

    def test_only_the_most_recent_result_is_kept(self):
        self.state.set_last_command_result(success=False, detail="first")
        self.clock += 5.0
        self.state.set_last_command_result(success=True, detail="second")
        result = self.state.last_command_result
        self.assertEqual(result.detail, "second")
        self.assertEqual(result.timestamp, _NOW + 5.0)


class NoEvaluationTests(StateTestCase):
    """State must never draw a conclusion. Policy owns every decision, and the
    emergency latch depends on State reporting exactly what it was told."""

    def test_a_critical_reading_does_not_raise_an_alarm(self):
        self.state.update_temperature("CPU", 500.0)
        self.assertEqual(self.state.alarms, set())

    def test_a_critical_reading_does_not_change_the_mode(self):
        self.state.update_temperature("CPU", 500.0)
        self.assertIs(self.state.mode, OperatingMode.STARTING)

    def test_a_critical_reading_does_not_request_a_fan_speed(self):
        self.state.update_temperature("CPU", 500.0)
        self.assertIsNone(self.state.requested_fan_speed)

    def test_a_failed_command_does_not_raise_an_alarm(self):
        self.state.set_last_command_result(success=False, detail="BMC gone")
        self.assertEqual(self.state.alarms, set())

    def test_clearing_the_last_sensor_does_not_change_the_mode(self):
        # "No readings" is an emergency, but it is Policy that says so.
        self.state.update_temperature("CPU", 50.0)
        self.state.set_mode(OperatingMode.RUNNING)
        self.state.clear_temperature("CPU")
        self.assertIs(self.state.mode, OperatingMode.RUNNING)


class ReadingTypeTests(unittest.TestCase):
    def test_readings_compare_by_value(self):
        self.assertEqual(
            TemperatureReading(value=40.0, timestamp=_NOW),
            TemperatureReading(value=40.0, timestamp=_NOW),
        )

    def test_command_results_compare_by_value(self):
        self.assertEqual(
            HardwareCommandResult(success=True, detail="ok", timestamp=_NOW),
            HardwareCommandResult(success=True, detail="ok", timestamp=_NOW),
        )

    def test_operating_modes_are_named_after_their_values(self):
        # The mode name is published in notification payloads and log lines.
        for mode in OperatingMode:
            with self.subTest(mode=mode):
                self.assertEqual(mode.name, mode.value)


if __name__ == "__main__":
    unittest.main()
