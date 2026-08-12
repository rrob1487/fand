"""Tests for lib/notifications/notification.py."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from lib.notifications.notification import Notification, SensorReading


def _notification(*readings: tuple[str, float], **overrides) -> Notification:
    defaults = dict(
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
    return Notification(**{**defaults, **overrides})


class SensorNamesTests(unittest.TestCase):
    def test_names_in_order(self):
        n = _notification(("CPU1 Temp", 80.0), ("CPU2 Temp", 75.0))
        self.assertEqual(n.sensor_names, ("CPU1 Temp", "CPU2 Temp"))

    def test_empty(self):
        self.assertEqual(_notification().sensor_names, ())


class HottestTests(unittest.TestCase):
    def test_returns_highest_reading(self):
        n = _notification(("CPU1 Temp", 80.0), ("GPU", 91.5), ("CPU2 Temp", 75.0))
        self.assertEqual(n.hottest.name, "GPU")
        self.assertEqual(n.hottest.value_c, 91.5)

    def test_none_when_no_readings(self):
        self.assertIsNone(_notification().hottest)

    def test_single_reading(self):
        self.assertEqual(_notification(("Only", 42.0)).hottest.name, "Only")

    def test_ties_return_a_reading(self):
        n = _notification(("A", 80.0), ("B", 80.0))
        self.assertIn(n.hottest.name, ("A", "B"))


class WithSensorsTests(unittest.TestCase):
    def setUp(self):
        self.n = _notification(
            ("CPU1 Temp", 80.0), ("CPU2 Temp", 75.0), ("n8n GPU", 91.0),
        )

    def test_none_selects_everything_and_returns_self(self):
        self.assertIs(self.n.with_sensors(None), self.n)

    def test_filters_to_named_sensors(self):
        filtered = self.n.with_sensors(("CPU1 Temp", "n8n GPU"))
        self.assertEqual(filtered.sensor_names, ("CPU1 Temp", "n8n GPU"))

    def test_preserves_configured_order_not_source_order(self):
        filtered = self.n.with_sensors(("n8n GPU", "CPU1 Temp"))
        self.assertEqual(filtered.sensor_names, ("n8n GPU", "CPU1 Temp"))

    def test_absent_names_are_dropped_silently(self):
        # The notifier reports the gap; the payload just carries what exists.
        filtered = self.n.with_sensors(("CPU1 Temp", "Nonexistent Temp"))
        self.assertEqual(filtered.sensor_names, ("CPU1 Temp",))

    def test_all_names_absent_yields_no_readings(self):
        filtered = self.n.with_sensors(("Nope", "Also Nope"))
        self.assertEqual(filtered.readings, ())

    def test_empty_selection(self):
        self.assertEqual(self.n.with_sensors(()).readings, ())

    def test_other_fields_are_carried_over(self):
        filtered = self.n.with_sensors(("CPU1 Temp",))
        self.assertEqual(filtered.timestamp, self.n.timestamp)
        self.assertEqual(filtered.fan_speed_percent, self.n.fan_speed_percent)
        self.assertEqual(filtered.operating_mode, self.n.operating_mode)
        self.assertEqual(filtered.last_command_ok, self.n.last_command_ok)

    def test_source_is_unchanged(self):
        self.n.with_sensors(("CPU1 Temp",))
        self.assertEqual(len(self.n.readings), 3)

    def test_hottest_reflects_the_filtered_set(self):
        # A notifier scoped to CPUs must not report the GPU as its hottest.
        filtered = self.n.with_sensors(("CPU1 Temp", "CPU2 Temp"))
        self.assertEqual(filtered.hottest.name, "CPU1 Temp")


class ImmutabilityTests(unittest.TestCase):
    """The payload crosses a thread boundary, so it must be a detached
    snapshot of primitives rather than a view of mutable State."""

    def test_notification_is_frozen(self):
        with self.assertRaises(FrozenInstanceError):
            _notification().timestamp = 0.0

    def test_reading_is_frozen(self):
        reading = SensorReading(name="CPU1 Temp", value_c=80.0, timestamp=0.0)
        with self.assertRaises(FrozenInstanceError):
            reading.value_c = 0.0

    def test_readings_are_a_tuple(self):
        self.assertIsInstance(_notification(("A", 1.0)).readings, tuple)


if __name__ == "__main__":
    unittest.main()
