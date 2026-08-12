"""Tests for lib/models/notification.py."""

from __future__ import annotations

import tomllib
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from lib.models.notification import (
    GeneralTriggerConfig,
    NotifierConfig,
    NotifierConfigError,
    ThresholdTriggerConfig,
)

_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config" / "notification"

# Minimal valid configuration. Tests copy it and mutate one key at a time so a
# failure names exactly the rule that broke.
_VALID = {
    "Name": "Test Notifier",
    "EndpointType": "discord",
    "Interval": 60,
    "QueueSize": 10,
    "Trigger": {"Type": "threshold", "Temperature": 80},
    "Credentials": {"Token": "FAND_TEST_TOKEN"},
}


def _config(**overrides):
    """_VALID with top-level keys replaced; a None value removes the key."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _VALID.items()}
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class ExampleFileTests(unittest.TestCase):
    """The shipped examples must parse — they are what operators copy."""

    def _load(self, filename: str) -> NotifierConfig:
        with open(_CONFIG_DIR / filename, "rb") as f:
            return NotifierConfig.from_dict(tomllib.load(f))

    def test_discord_example(self):
        config = self._load("discord.toml.example")
        self.assertEqual(config.endpoint_type, "discord")
        self.assertEqual(config.interval_seconds, 60.0)
        self.assertEqual(config.queue_size, 10)
        self.assertEqual(config.max_attempts, 3)
        self.assertEqual(config.retry_backoff_seconds, 1.0)
        self.assertIsInstance(config.trigger, ThresholdTriggerConfig)
        self.assertEqual(config.trigger.temperature_c, 80.0)
        self.assertIsNone(config.trigger.sensors)
        self.assertEqual(config.credentials["Token"], "FAND_DISCORD_TOKEN")
        self.assertEqual(config.endpoint_options["Timeout"], 10.0)

    def test_homeassistant_example(self):
        config = self._load("homeassistant.toml.example")
        self.assertEqual(config.endpoint_type, "homeassistant")
        self.assertEqual(config.interval_seconds, 30.0)
        self.assertEqual(config.queue_size, 100)
        self.assertIsInstance(config.trigger, GeneralTriggerConfig)
        self.assertEqual(
            config.trigger.sensors, ("CPU1 Temp", "CPU2 Temp", "n8n GPU"),
        )
        self.assertEqual(config.endpoint_options["EntityPrefix"], "fand")

    def test_examples_omitting_optional_delivery_keys_use_defaults(self):
        # homeassistant.toml.example sets neither MaxAttempts nor RetryBackoff.
        config = self._load("homeassistant.toml.example")
        self.assertEqual(config.max_attempts, 3)
        self.assertEqual(config.retry_backoff_seconds, 1.0)
        self.assertTrue(config.enabled)


class DefaultsTests(unittest.TestCase):
    def test_enabled_defaults_true_when_omitted(self):
        self.assertTrue(NotifierConfig.from_dict(_config()).enabled)

    def test_enabled_honoured_when_present(self):
        self.assertFalse(NotifierConfig.from_dict(_config(Enabled=False)).enabled)

    def test_delivery_defaults(self):
        config = NotifierConfig.from_dict(_config())
        self.assertEqual(config.max_attempts, 3)
        self.assertEqual(config.retry_backoff_seconds, 1.0)

    def test_endpoint_options_default_to_empty(self):
        self.assertEqual(dict(NotifierConfig.from_dict(_config()).endpoint_options), {})

    def test_integers_are_coerced_to_float(self):
        # TOML gives `Interval = 60` as an int; the model stores seconds.
        config = NotifierConfig.from_dict(_config())
        self.assertIsInstance(config.interval_seconds, float)
        self.assertIsInstance(config.trigger.temperature_c, float)


class RequiredKeyTests(unittest.TestCase):
    def test_each_required_key_is_required(self):
        for key in ("Name", "EndpointType", "Interval", "QueueSize", "Trigger", "Credentials"):
            with self.subTest(key=key), self.assertRaises(NotifierConfigError) as ctx:
                NotifierConfig.from_dict(_config(**{key: None}))
            self.assertIn(key, str(ctx.exception))

    def test_trigger_type_required(self):
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(_config(Trigger={"Temperature": 80}))
        self.assertIn("[Trigger].Type", str(ctx.exception))

    def test_threshold_temperature_required(self):
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(_config(Trigger={"Type": "threshold"}))
        self.assertIn("Temperature", str(ctx.exception))

    def test_threshold_temperature_must_not_be_negative(self):
        # A negative threshold is satisfied by every reading a server sensor
        # can produce, which is a general trigger written as a threshold.
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(
                _config(Trigger={"Type": "threshold", "Temperature": -1})
            )
        self.assertIn("Temperature", str(ctx.exception))

    def test_threshold_temperature_zero_is_allowed(self):
        config = NotifierConfig.from_dict(
            _config(Trigger={"Type": "threshold", "Temperature": 0})
        )
        self.assertEqual(config.trigger.temperature_c, 0.0)


class ValueValidationTests(unittest.TestCase):
    def _reject(self, **overrides):
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(_config(**overrides))
        return str(ctx.exception)

    def test_name_must_be_non_empty_string(self):
        for bad in ("", "   ", 5, []):
            with self.subTest(value=bad):
                self.assertIn("Name", self._reject(Name=bad))

    def test_endpoint_type_must_be_non_empty_string(self):
        self.assertIn("EndpointType", self._reject(EndpointType=""))

    def test_endpoint_type_membership_is_not_checked_here(self):
        # Which endpoints exist is the factory's knowledge, not the model's.
        config = NotifierConfig.from_dict(_config(EndpointType="not-a-real-endpoint"))
        self.assertEqual(config.endpoint_type, "not-a-real-endpoint")

    def test_interval_must_be_positive(self):
        for bad in (0, -1, "60"):
            with self.subTest(value=bad):
                self.assertIn("Interval", self._reject(Interval=bad))

    def test_interval_rejects_bool(self):
        # bool is a subclass of int; `Interval = true` must not read as 1.
        self.assertIn("Interval", self._reject(Interval=True))

    def test_queue_size_bounds(self):
        for bad in (0, -1, 10_001, 1.5):
            with self.subTest(value=bad):
                self.assertIn("QueueSize", self._reject(QueueSize=bad))

    def test_queue_size_bounds_are_inclusive(self):
        for good in (1, 10_000):
            with self.subTest(value=good):
                config = NotifierConfig.from_dict(_config(QueueSize=good))
                self.assertEqual(config.queue_size, good)

    def test_max_attempts_must_be_at_least_one(self):
        self.assertIn("MaxAttempts", self._reject(MaxAttempts=0))

    def test_retry_backoff_must_not_be_negative(self):
        self.assertIn("RetryBackoff", self._reject(RetryBackoff=-1))

    def test_retry_backoff_zero_is_allowed(self):
        self.assertEqual(
            NotifierConfig.from_dict(_config(RetryBackoff=0)).retry_backoff_seconds, 0.0,
        )

    def test_enabled_must_be_boolean(self):
        self.assertIn("Enabled", self._reject(Enabled="yes"))

    def test_unknown_trigger_type(self):
        message = self._reject(Trigger={"Type": "whenever"})
        self.assertIn("whenever", message)
        self.assertIn("threshold", message)
        self.assertIn("general", message)

    def test_tables_must_be_tables(self):
        self.assertIn("[Trigger]", self._reject(Trigger="threshold"))
        self.assertIn("[Credentials]", self._reject(Credentials="FAND_TEST_TOKEN"))
        self.assertIn("[Endpoint]", self._reject(Endpoint=10.0))


class SensorSelectionTests(unittest.TestCase):
    def test_omitted_means_all_sensors(self):
        config = NotifierConfig.from_dict(_config())
        self.assertIsNone(config.trigger.sensors)

    def test_preserved_as_ordered_tuple(self):
        config = NotifierConfig.from_dict(
            _config(Trigger={"Type": "general", "Sensors": ["CPU2 Temp", "CPU1 Temp"]})
        )
        self.assertEqual(config.trigger.sensors, ("CPU2 Temp", "CPU1 Temp"))

    def test_allowed_on_threshold_triggers(self):
        config = NotifierConfig.from_dict(
            _config(Trigger={"Type": "threshold", "Temperature": 80, "Sensors": ["CPU1 Temp"]})
        )
        self.assertEqual(config.trigger.sensors, ("CPU1 Temp",))

    def test_rejects_non_list(self):
        with self.assertRaises(NotifierConfigError):
            NotifierConfig.from_dict(
                _config(Trigger={"Type": "general", "Sensors": "CPU1 Temp"})
            )

    def test_rejects_empty_list(self):
        # An empty list would silently make a threshold trigger never fire.
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(_config(Trigger={"Type": "general", "Sensors": []}))
        self.assertIn("omit it", str(ctx.exception))

    def test_rejects_empty_entry(self):
        with self.assertRaises(NotifierConfigError):
            NotifierConfig.from_dict(
                _config(Trigger={"Type": "general", "Sensors": ["CPU1 Temp", ""]})
            )


class UnknownKeyTests(unittest.TestCase):
    def test_unknown_top_level_key(self):
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(_config(MaxAttempt=5))
        self.assertIn("MaxAttempt", str(ctx.exception))

    def test_unknown_trigger_key(self):
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(
                _config(Trigger={"Type": "threshold", "Temperature": 80, "Sensor": ["x"]})
            )
        self.assertIn("Sensor", str(ctx.exception))

    def test_temperature_on_general_trigger_is_rejected(self):
        # A threshold notifier whose Type was typed wrong.
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(_config(Trigger={"Type": "general", "Temperature": 80}))
        self.assertIn("Temperature", str(ctx.exception))

    def test_endpoint_and_credentials_stay_opaque(self):
        # Their schemas belong to the endpoint implementation, not the model.
        config = NotifierConfig.from_dict(
            _config(
                Endpoint={"SomeFutureOption": 1},
                Credentials={"AnyKeyTheEndpointWants": "FAND_SOMETHING"},
            )
        )
        self.assertEqual(config.endpoint_options["SomeFutureOption"], 1)
        self.assertEqual(config.credentials["AnyKeyTheEndpointWants"], "FAND_SOMETHING")


class CredentialTests(unittest.TestCase):
    def test_accepts_environment_variable_names(self):
        config = NotifierConfig.from_dict(
            _config(Credentials={"Token": "FAND_X", "Url": "_private2"})
        )
        self.assertEqual(dict(config.credentials), {"Token": "FAND_X", "Url": "_private2"})

    def test_rejects_empty_table(self):
        with self.assertRaises(NotifierConfigError):
            NotifierConfig.from_dict(_config(Credentials={}))

    def test_rejects_values_that_are_not_variable_names(self):
        for bad in ("https://ha.local", "abc-def", "has space", "1LEADING_DIGIT", "", 42):
            with self.subTest(value=bad):
                with self.assertRaises(NotifierConfigError):
                    NotifierConfig.from_dict(_config(Credentials={"Token": bad}))

    def test_rejection_message_never_contains_the_value(self):
        """The whole point of the check: a failing value is probably the secret
        itself, and this message goes straight to the log."""
        secret = "MTk4NzY1NDMyMTA5.Gh3Kx9.super-secret-bot-token"
        with self.assertRaises(NotifierConfigError) as ctx:
            NotifierConfig.from_dict(_config(Credentials={"Token": secret}))
        message = str(ctx.exception)
        self.assertNotIn(secret, message)
        self.assertIn("Token", message)

    def test_repr_never_contains_a_secret(self):
        # Only variable names are ever stored, so a repr cannot leak one.
        config = NotifierConfig.from_dict(_config())
        self.assertIn("FAND_TEST_TOKEN", repr(config))
        self.assertNotIn("super-secret", repr(config))


class EqualityTests(unittest.TestCase):
    """Phase 9's reload reconciliation leaves a notifier running when its
    configuration is unchanged, which relies on value equality."""

    def test_identical_input_compares_equal(self):
        self.assertEqual(
            NotifierConfig.from_dict(_config()), NotifierConfig.from_dict(_config()),
        )

    def test_differing_input_compares_unequal(self):
        base = NotifierConfig.from_dict(_config())
        for key, value in (
            ("Name", "Renamed"),
            ("Interval", 30),
            ("QueueSize", 11),
            ("MaxAttempts", 5),
        ):
            with self.subTest(key=key):
                self.assertNotEqual(base, NotifierConfig.from_dict(_config(**{key: value})))

    def test_trigger_difference_compares_unequal(self):
        self.assertNotEqual(
            NotifierConfig.from_dict(_config()),
            NotifierConfig.from_dict(
                _config(Trigger={"Type": "threshold", "Temperature": 85})
            ),
        )

    def test_credential_difference_compares_unequal(self):
        self.assertNotEqual(
            NotifierConfig.from_dict(_config()),
            NotifierConfig.from_dict(_config(Credentials={"Token": "FAND_OTHER"})),
        )

    def test_key_order_does_not_affect_equality(self):
        reordered = _config()
        reordered["Credentials"] = {"B": "FAND_B", "A": "FAND_A"}
        other = _config()
        other["Credentials"] = {"A": "FAND_A", "B": "FAND_B"}
        self.assertEqual(
            NotifierConfig.from_dict(reordered), NotifierConfig.from_dict(other),
        )


class ImmutabilityTests(unittest.TestCase):
    def test_dataclass_is_frozen(self):
        config = NotifierConfig.from_dict(_config())
        with self.assertRaises(FrozenInstanceError):
            config.name = "changed"

    def test_credentials_cannot_be_mutated(self):
        config = NotifierConfig.from_dict(_config())
        with self.assertRaises(TypeError):
            config.credentials["Token"] = "FAND_HIJACKED"

    def test_endpoint_options_cannot_be_mutated(self):
        config = NotifierConfig.from_dict(_config(Endpoint={"Timeout": 10.0}))
        with self.assertRaises(TypeError):
            config.endpoint_options["Timeout"] = 0.0

    def test_source_mapping_is_copied(self):
        # Mutating the parsed TOML afterwards must not change the model.
        data = _config(Endpoint={"Timeout": 10.0})
        config = NotifierConfig.from_dict(data)
        data["Endpoint"]["Timeout"] = 999.0
        self.assertEqual(config.endpoint_options["Timeout"], 10.0)


if __name__ == "__main__":
    unittest.main()
