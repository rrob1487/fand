"""Tests for lib/models/config.py.

Pure dict-in, dataclass-out. The error *type* matters as much as the values:
ConfigManager converts KeyError into ConfigError, and Daemon.reload_config only
survives a bad edit when the failure arrives as ConfigError.
"""

from __future__ import annotations

import pathlib
import tomllib
import unittest

from lib.models.config import (
    Config,
    DaemonConfig,
    FanCurveConfig,
    FanCurvePoint,
    SafetyConfig,
    WatchdogConfig,
)

_CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"

_VALID = {
    "daemon": {"poll_interval": 5, "log_level": "INFO"},
    "fan_curve": {"points": [[40, 20], [85, 100]], "hysteresis_percent": 5},
    "safety": {
        "max_temperature": 90,
        "shutdown_on_emergency": True,
        "recovery_margin_c": 5,
    },
    "watchdog": {"enabled": True},
}


def _config(**overrides) -> dict:
    """_VALID with top-level tables replaced; a None value removes the table."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _VALID.items()}
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class DaemonConfigTests(unittest.TestCase):
    def test_values_are_read(self):
        config = DaemonConfig.from_dict({"poll_interval": 5, "log_level": "DEBUG"})
        self.assertEqual(config.poll_interval, 5)
        self.assertEqual(config.log_level, "DEBUG")

    def test_a_missing_poll_interval_raises_key_error(self):
        with self.assertRaises(KeyError):
            DaemonConfig.from_dict({"log_level": "INFO"})

    def test_a_missing_log_level_raises_key_error(self):
        with self.assertRaises(KeyError):
            DaemonConfig.from_dict({"poll_interval": 5})

    def test_the_rediscover_interval_defaults(self):
        # Optional so an existing config.toml keeps working untouched.
        config = DaemonConfig.from_dict({"poll_interval": 5, "log_level": "INFO"})
        self.assertEqual(config.sensor_rediscover_interval, 300.0)

    def test_the_rediscover_interval_can_be_set(self):
        config = DaemonConfig.from_dict(
            {"poll_interval": 5, "log_level": "INFO", "sensor_rediscover_interval": 60},
        )
        self.assertEqual(config.sensor_rediscover_interval, 60)


class FanCurveConfigTests(unittest.TestCase):
    def test_points_become_typed_pairs(self):
        config = FanCurveConfig.from_dict({"points": [[40, 20], [85, 100]]})
        self.assertEqual(
            config.points,
            (
                FanCurvePoint(temperature_c=40, fan_percent=20),
                FanCurvePoint(temperature_c=85, fan_percent=100),
            ),
        )

    def test_points_are_a_tuple(self):
        # Frozen config must not hand out a list callers could mutate.
        config = FanCurveConfig.from_dict({"points": [[40, 20]]})
        self.assertIsInstance(config.points, tuple)

    def test_hysteresis_defaults_to_five_percent(self):
        self.assertEqual(FanCurveConfig.from_dict({"points": []}).hysteresis_percent, 5.0)

    def test_hysteresis_can_be_set(self):
        config = FanCurveConfig.from_dict({"points": [], "hysteresis_percent": 2})
        self.assertEqual(config.hysteresis_percent, 2)

    def test_an_omitted_points_key_yields_an_empty_curve(self):
        # Policy treats an empty curve as "run at 100%", so this is survivable
        # rather than fatal.
        self.assertEqual(FanCurveConfig.from_dict({}).points, ())

    def test_point_order_is_preserved_as_written(self):
        # Policy sorts before interpolating, so the model does not need to.
        config = FanCurveConfig.from_dict({"points": [[85, 100], [40, 20]]})
        self.assertEqual(config.points[0].temperature_c, 85)

    def test_a_malformed_point_does_not_raise_key_error(self):
        # Worth pinning because of what it means downstream: ConfigManager only
        # converts KeyError into ConfigError, so this escapes load() as a raw
        # TypeError and Daemon.reload_config's "keep the previous config"
        # guard, which catches only ConfigError, does not cover it.
        with self.assertRaises(TypeError):
            FanCurveConfig.from_dict({"points": [40, 20]})


class SafetyConfigTests(unittest.TestCase):
    def test_the_maximum_temperature_is_required(self):
        with self.assertRaises(KeyError):
            SafetyConfig.from_dict({})

    def test_shutdown_defaults_to_off(self):
        # Powering the host off is opt-in, never a default.
        config = SafetyConfig.from_dict({"max_temperature": 90})
        self.assertFalse(config.shutdown_on_emergency)

    def test_the_recovery_margin_defaults_to_zero(self):
        config = SafetyConfig.from_dict({"max_temperature": 90})
        self.assertEqual(config.recovery_margin_c, 0.0)

    def test_optional_values_are_read_when_present(self):
        config = SafetyConfig.from_dict(
            {"max_temperature": 90, "shutdown_on_emergency": True, "recovery_margin_c": 5},
        )
        self.assertTrue(config.shutdown_on_emergency)
        self.assertEqual(config.recovery_margin_c, 5)


class WatchdogConfigTests(unittest.TestCase):
    def test_enabled_is_required(self):
        with self.assertRaises(KeyError):
            WatchdogConfig.from_dict({})

    def test_enabled_is_read(self):
        self.assertFalse(WatchdogConfig.from_dict({"enabled": False}).enabled)


class ConfigTests(unittest.TestCase):
    def test_every_section_is_built(self):
        config = Config.from_dict(_config())
        self.assertIsInstance(config.daemon, DaemonConfig)
        self.assertIsInstance(config.fan_curve, FanCurveConfig)
        self.assertIsInstance(config.safety, SafetyConfig)
        self.assertIsInstance(config.watchdog, WatchdogConfig)

    def test_values_survive_the_round_trip(self):
        config = Config.from_dict(_config())
        self.assertEqual(config.daemon.poll_interval, 5)
        self.assertEqual(config.safety.max_temperature, 90)
        self.assertEqual(len(config.fan_curve.points), 2)

    def test_every_section_is_required(self):
        for section in ("daemon", "fan_curve", "safety", "watchdog"):
            with self.subTest(section=section):
                with self.assertRaises(KeyError):
                    Config.from_dict(_config(**{section: None}))

    def test_a_missing_nested_key_still_raises_key_error(self):
        # ConfigManager reports the key name back to the operator, so the
        # KeyError has to survive the nesting.
        with self.assertRaises(KeyError):
            Config.from_dict(_config(safety={}))


class ImmutabilityTests(unittest.TestCase):
    """Config is read once and shared with Policy; nothing downstream may edit
    it in place and change the machine's behaviour mid-run."""

    def test_the_config_is_frozen(self):
        config = Config.from_dict(_config())
        with self.assertRaises(Exception):
            config.daemon = None

    def test_every_section_is_frozen(self):
        config = Config.from_dict(_config())
        for section, attribute in (
            (config.daemon, "poll_interval"),
            (config.fan_curve, "hysteresis_percent"),
            (config.safety, "max_temperature"),
            (config.watchdog, "enabled"),
        ):
            with self.subTest(section=type(section).__name__):
                with self.assertRaises(Exception):
                    setattr(section, attribute, 0)

    def test_a_curve_point_is_frozen(self):
        point = FanCurvePoint(temperature_c=40, fan_percent=20)
        with self.assertRaises(Exception):
            point.fan_percent = 100


class EqualityTests(unittest.TestCase):
    def test_identical_configs_compare_equal(self):
        self.assertEqual(Config.from_dict(_config()), Config.from_dict(_config()))

    def test_a_changed_value_compares_unequal(self):
        other = _config(daemon={"poll_interval": 10, "log_level": "INFO"})
        self.assertNotEqual(Config.from_dict(_config()), Config.from_dict(other))

    def test_curve_points_are_hashable(self):
        # Frozen dataclasses in a tuple: usable in sets and as dict keys.
        self.assertEqual(
            len({FanCurvePoint(40, 20), FanCurvePoint(40, 20), FanCurvePoint(50, 30)}), 2,
        )


class ExampleFileTests(unittest.TestCase):
    """The shipped example must parse -- it is what operators copy."""

    def setUp(self):
        with open(_CONFIG_DIR / "config.toml.example", "rb") as handle:
            self.config = Config.from_dict(tomllib.load(handle))

    def test_the_example_parses(self):
        self.assertEqual(self.config.daemon.poll_interval, 5)
        self.assertEqual(self.config.daemon.log_level, "INFO")

    def test_the_example_curve_is_ordered_and_rising(self):
        points = self.config.fan_curve.points
        self.assertEqual(
            [p.temperature_c for p in points], sorted(p.temperature_c for p in points),
        )
        self.assertEqual(
            [p.fan_percent for p in points], sorted(p.fan_percent for p in points),
        )

    def test_the_example_curve_tops_out_at_full_speed(self):
        self.assertEqual(max(p.fan_percent for p in self.config.fan_curve.points), 100)

    def test_the_example_leaves_a_warning_band(self):
        # The file's own comment promises this: max_temperature must sit above
        # the curve's top point, or there is no WARNING band between "curve
        # maxed out" and EMERGENCY.
        curve_top = max(p.temperature_c for p in self.config.fan_curve.points)
        self.assertGreater(self.config.safety.max_temperature, curve_top)

    def test_the_example_does_not_power_the_host_off(self):
        # A shipped default that shuts the machine down would be a nasty
        # surprise for anyone who copied it unread.
        self.assertFalse(self.config.safety.shutdown_on_emergency)

    def test_the_example_sets_a_recovery_margin(self):
        self.assertGreater(self.config.safety.recovery_margin_c, 0)

    def test_the_example_matches_the_shipped_unit_file(self):
        # fand.service is Type=notify with WatchdogSec set, so the example must
        # not ship with the watchdog disabled.
        self.assertTrue(self.config.watchdog.enabled)


if __name__ == "__main__":
    unittest.main()
