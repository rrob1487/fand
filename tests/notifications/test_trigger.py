"""Tests for lib/notifications/trigger.py."""

from __future__ import annotations

import unittest

from lib.notifications.notification import Notification, SensorReading
from lib.notifications.trigger import GeneralTrigger, ThresholdTrigger, Trigger


def _notification(*readings: tuple[str, float]) -> Notification:
    return Notification(
        timestamp=1_700_000_000.0,
        readings=tuple(
            SensorReading(name=n, value_c=v, timestamp=1_700_000_000.0)
            for n, v in readings
        ),
        fan_speed_percent=70.0,
        operating_mode="RUNNING",
        alarms=(),
        last_command_ok=True,
    )


class ThresholdBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.trigger = ThresholdTrigger(sensors=None, temperature_c=80.0)

    def test_above_threshold_is_active(self):
        self.assertTrue(self.trigger.is_active(_notification(("CPU1 Temp", 80.1))))

    def test_exactly_at_threshold_is_active(self):
        # The comparison is >=, per notification.md.
        self.assertTrue(self.trigger.is_active(_notification(("CPU1 Temp", 80.0))))

    def test_below_threshold_is_inactive(self):
        self.assertFalse(self.trigger.is_active(_notification(("CPU1 Temp", 79.9))))

    def test_zero_threshold(self):
        trigger = ThresholdTrigger(sensors=None, temperature_c=0.0)
        self.assertTrue(trigger.is_active(_notification(("CPU1 Temp", 0.0))))
        self.assertFalse(trigger.is_active(_notification(("CPU1 Temp", -0.1))))


class ThresholdWithoutReadingsTests(unittest.TestCase):
    """A notifier that cannot see a temperature cannot claim a threshold was
    crossed."""

    def test_no_readings_at_all(self):
        trigger = ThresholdTrigger(sensors=None, temperature_c=80.0)
        self.assertFalse(trigger.is_active(_notification()))

    def test_no_readings_match_the_selection(self):
        trigger = ThresholdTrigger(sensors=("Missing Temp",), temperature_c=80.0)
        self.assertFalse(trigger.is_active(_notification(("CPU1 Temp", 95.0))))

    def test_some_selected_names_missing_still_evaluates_the_rest(self):
        trigger = ThresholdTrigger(
            sensors=("Missing Temp", "CPU1 Temp"), temperature_c=80.0,
        )
        self.assertTrue(trigger.is_active(_notification(("CPU1 Temp", 95.0))))


class ThresholdScopingTests(unittest.TestCase):
    """The completion criteria for this phase."""

    def test_hotter_sensor_outside_the_selection_is_ignored(self):
        # The GPU is well past the threshold, but this notifier watches CPUs.
        trigger = ThresholdTrigger(
            sensors=("CPU1 Temp", "CPU2 Temp"), temperature_c=80.0,
        )
        notification = _notification(
            ("CPU1 Temp", 60.0), ("CPU2 Temp", 62.0), ("n8n GPU", 95.0),
        )
        self.assertFalse(trigger.is_active(notification))

    def test_selected_sensor_crossing_activates_despite_cooler_others(self):
        trigger = ThresholdTrigger(sensors=("n8n GPU",), temperature_c=80.0)
        notification = _notification(
            ("CPU1 Temp", 60.0), ("CPU2 Temp", 62.0), ("n8n GPU", 95.0),
        )
        self.assertTrue(trigger.is_active(notification))

    def test_omitted_selection_evaluates_every_reading(self):
        trigger = ThresholdTrigger(sensors=None, temperature_c=80.0)
        notification = _notification(
            ("CPU1 Temp", 60.0), ("CPU2 Temp", 62.0), ("n8n GPU", 95.0),
        )
        self.assertTrue(trigger.is_active(notification))

    def test_evaluation_does_not_mutate_the_snapshot(self):
        trigger = ThresholdTrigger(sensors=("CPU1 Temp",), temperature_c=80.0)
        notification = _notification(("CPU1 Temp", 95.0), ("n8n GPU", 40.0))
        trigger.is_active(notification)
        self.assertEqual(len(notification.readings), 2)


class GeneralTriggerTests(unittest.TestCase):
    def test_active_with_readings(self):
        trigger = GeneralTrigger(sensors=None)
        self.assertTrue(trigger.is_active(_notification(("CPU1 Temp", 20.0))))

    def test_active_without_readings(self):
        # Unlike a threshold trigger, this one does not depend on sensor data.
        self.assertTrue(GeneralTrigger(sensors=None).is_active(_notification()))

    def test_active_regardless_of_temperature(self):
        trigger = GeneralTrigger(sensors=None)
        for temperature in (-40.0, 0.0, 20.0, 95.0):
            with self.subTest(temperature=temperature):
                self.assertTrue(trigger.is_active(_notification(("T", temperature))))

    def test_active_even_when_its_selection_is_absent(self):
        trigger = GeneralTrigger(sensors=("Missing Temp",))
        self.assertTrue(trigger.is_active(_notification(("CPU1 Temp", 50.0))))


class InterfaceTests(unittest.TestCase):
    def test_base_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Trigger(sensors=None)

    def test_subclass_must_implement_is_active(self):
        class Incomplete(Trigger):
            pass

        with self.assertRaises(TypeError):
            Incomplete(sensors=None)

    def test_sensor_names_round_trips(self):
        names = ("CPU1 Temp", "CPU2 Temp")
        self.assertEqual(ThresholdTrigger(names, 80.0).sensor_names, names)
        self.assertEqual(GeneralTrigger(names).sensor_names, names)

    def test_sensor_names_none_means_all(self):
        self.assertIsNone(ThresholdTrigger(None, 80.0).sensor_names)
        self.assertIsNone(GeneralTrigger(None).sensor_names)

    def test_both_types_share_the_interface(self):
        # Phase 6 holds triggers as Trigger and never branches on their type.
        for trigger in (ThresholdTrigger(None, 80.0), GeneralTrigger(None)):
            with self.subTest(trigger=type(trigger).__name__):
                self.assertIsInstance(trigger, Trigger)
                self.assertIsInstance(trigger.is_active(_notification()), bool)


if __name__ == "__main__":
    unittest.main()
