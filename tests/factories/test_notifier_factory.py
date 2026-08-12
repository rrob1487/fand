"""Tests for lib/factories/notifier_factory.py."""

from __future__ import annotations

import os
import pathlib
import tomllib
import unittest
from unittest.mock import patch

from lib.factories.notifier_factory import (
    ENDPOINT_BUILDERS,
    create_endpoint,
    create_notifier,
    create_trigger,
)
from lib.models.notification import NotifierConfig, NotifierConfigError
from lib.notifications.discord import DiscordEndpoint
from lib.notifications.homeassistant import HomeAssistantEndpoint
from lib.notifications.notification import Notification, SensorReading
from lib.notifications.notifier import Notifier
from lib.notifications.trigger import GeneralTrigger, ThresholdTrigger
from tests.support.http_server import LoopbackServerTestCase

_SECRET = "MTk4NzY1NDMyMTA5.Gh3Kx9.super-secret-bot-token"

_ENV = {
    "FAND_DISCORD_TOKEN": _SECRET,
    "FAND_DISCORD_SERVER": "998877665544332211",
    "FAND_DISCORD_CHANNEL": "112233445566778899",
    "FAND_HOMEASSISTANT_URL": "https://ha.local:8123",
    "FAND_HOMEASSISTANT_TOKEN": "eyJhbGciOiJIUzI1NiJ9.ha-secret",
}

_DISCORD = {
    "Name": "Discord Temperature Alerts",
    "EndpointType": "discord",
    "Interval": 60,
    "QueueSize": 10,
    "Trigger": {"Type": "threshold", "Temperature": 80},
    "Credentials": {
        "Token": "FAND_DISCORD_TOKEN",
        "Server": "FAND_DISCORD_SERVER",
        "Channel": "FAND_DISCORD_CHANNEL",
    },
}

_HOMEASSISTANT = {
    "Name": "Home Assistant Sensors",
    "EndpointType": "homeassistant",
    "Interval": 30,
    "QueueSize": 100,
    "Trigger": {"Type": "general"},
    "Credentials": {
        "URL": "FAND_HOMEASSISTANT_URL",
        "Token": "FAND_HOMEASSISTANT_TOKEN",
    },
}


def _config(base=None, **overrides) -> NotifierConfig:
    data = {k: (dict(v) if isinstance(v, dict) else v)
            for k, v in (base or _DISCORD).items()}
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return NotifierConfig.from_dict(data)


class FactoryTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, _ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)


class RegistryTests(FactoryTestCase):
    def test_both_endpoint_types_registered(self):
        self.assertEqual(sorted(ENDPOINT_BUILDERS), ["discord", "homeassistant"])

    def test_unknown_endpoint_type_is_rejected(self):
        with self.assertRaises(NotifierConfigError) as ctx:
            create_endpoint(_config(EndpointType="slack"))
        message = str(ctx.exception)
        self.assertIn("slack", message)
        self.assertIn("discord", message)
        self.assertIn("homeassistant", message)

    def test_model_does_not_check_membership(self):
        # The registry defines which types exist, so the model accepts any
        # non-empty string and the factory is what rejects it.
        _config(EndpointType="slack")


class CredentialResolutionTests(FactoryTestCase):
    def test_discord_credentials_reach_the_endpoint(self):
        endpoint = create_endpoint(_config())
        self.assertIsInstance(endpoint, DiscordEndpoint)
        self.assertEqual(endpoint._token, _SECRET)
        self.assertEqual(endpoint._channel_id, "112233445566778899")
        self.assertEqual(endpoint._server_id, "998877665544332211")

    def test_homeassistant_credentials_reach_the_endpoint(self):
        endpoint = create_endpoint(_config(_HOMEASSISTANT))
        self.assertIsInstance(endpoint, HomeAssistantEndpoint)
        self.assertEqual(endpoint._base_url, "https://ha.local:8123")
        self.assertEqual(endpoint._token, "eyJhbGciOiJIUzI1NiJ9.ha-secret")

    def test_server_is_optional_for_discord(self):
        config = _config(Credentials={
            "Token": "FAND_DISCORD_TOKEN", "Channel": "FAND_DISCORD_CHANNEL",
        })
        self.assertIsNone(create_endpoint(config)._server_id)

    def test_missing_required_credential_key(self):
        config = _config(Credentials={"Token": "FAND_DISCORD_TOKEN"})
        with self.assertRaises(NotifierConfigError) as ctx:
            create_endpoint(config)
        self.assertIn("Channel", str(ctx.exception))

    def test_unknown_credential_key(self):
        config = _config(Credentials={
            "Token": "FAND_DISCORD_TOKEN",
            "Channel": "FAND_DISCORD_CHANNEL",
            "Webhook": "FAND_SOMETHING",
        })
        with self.assertRaises(NotifierConfigError) as ctx:
            create_endpoint(config)
        self.assertIn("Webhook", str(ctx.exception))


class MissingEnvironmentTests(FactoryTestCase):
    """The completion criterion: names the variable, never a secret."""

    def test_unset_variable_names_the_variable(self):
        config = _config(Credentials={
            "Token": "FAND_NOT_SET_ANYWHERE",
            "Channel": "FAND_DISCORD_CHANNEL",
        })
        with self.assertRaises(NotifierConfigError) as ctx:
            create_endpoint(config)
        message = str(ctx.exception)
        self.assertIn("FAND_NOT_SET_ANYWHERE", message)
        self.assertIn("Token", message)

    def test_empty_variable_is_treated_as_unset(self):
        # An empty token is a misconfiguration, not a valid credential.
        with patch.dict(os.environ, {"FAND_DISCORD_TOKEN": "   "}):
            with self.assertRaises(NotifierConfigError) as ctx:
                create_endpoint(_config())
        self.assertIn("FAND_DISCORD_TOKEN", str(ctx.exception))

    def test_message_never_contains_the_secret(self):
        # Every failure path, with a real secret sitting in the environment.
        cases = [
            _config(Credentials={"Token": "FAND_MISSING", "Channel": "FAND_DISCORD_CHANNEL"}),
            _config(Credentials={"Token": "FAND_DISCORD_TOKEN"}),
            _config(Credentials={
                "Token": "FAND_DISCORD_TOKEN",
                "Channel": "FAND_DISCORD_CHANNEL",
                "Extra": "FAND_DISCORD_TOKEN",
            }),
            _config(EndpointType="nope"),
        ]
        for index, config in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(NotifierConfigError) as ctx:
                    create_endpoint(config)
                self.assertNotIn(_SECRET, str(ctx.exception))
                self.assertNotIn("super-secret", str(ctx.exception))


class EndpointOptionTests(FactoryTestCase):
    def test_timeout_is_applied(self):
        endpoint = create_endpoint(_config(Endpoint={"Timeout": 2.5}))
        self.assertEqual(endpoint._timeout, 2.5)

    def test_entity_prefix_is_applied(self):
        endpoint = create_endpoint(_config(_HOMEASSISTANT, Endpoint={"EntityPrefix": "rack1"}))
        self.assertEqual(endpoint._prefix, "rack1")

    def test_defaults_apply_when_the_table_is_absent(self):
        self.assertEqual(create_endpoint(_config())._timeout, 10.0)
        self.assertEqual(create_endpoint(_config(_HOMEASSISTANT))._prefix, "fand")

    def test_unknown_option_is_rejected(self):
        # The model passes [Endpoint] through opaquely, so the factory is the
        # first thing that can catch a typo here.
        with self.assertRaises(NotifierConfigError) as ctx:
            create_endpoint(_config(Endpoint={"Timout": 5.0}))
        self.assertIn("Timout", str(ctx.exception))

    def test_option_valid_for_another_endpoint_is_still_rejected(self):
        with self.assertRaises(NotifierConfigError) as ctx:
            create_endpoint(_config(Endpoint={"EntityPrefix": "rack1"}))
        self.assertIn("EntityPrefix", str(ctx.exception))

    def test_timeout_must_be_a_positive_number(self):
        for bad in (0, -1, "10", True):
            with self.subTest(value=bad):
                with self.assertRaises(NotifierConfigError):
                    create_endpoint(_config(Endpoint={"Timeout": bad}))

    def test_entity_prefix_must_be_a_non_empty_string(self):
        for bad in ("", "  ", 5):
            with self.subTest(value=bad):
                with self.assertRaises(NotifierConfigError):
                    create_endpoint(_config(_HOMEASSISTANT, Endpoint={"EntityPrefix": bad}))


class TriggerConstructionTests(FactoryTestCase):
    def test_threshold_trigger(self):
        trigger = create_trigger(_config().trigger)
        self.assertIsInstance(trigger, ThresholdTrigger)
        self.assertIsNone(trigger.sensor_names)

    def test_general_trigger(self):
        self.assertIsInstance(create_trigger(_config(_HOMEASSISTANT).trigger), GeneralTrigger)

    def test_sensor_selection_is_carried_through(self):
        config = _config(Trigger={
            "Type": "threshold", "Temperature": 80, "Sensors": ["CPU1 Temp"],
        })
        self.assertEqual(create_trigger(config.trigger).sensor_names, ("CPU1 Temp",))

    def test_threshold_temperature_is_carried_through(self):
        config = _config(Trigger={"Type": "threshold", "Temperature": 91})
        self.assertEqual(create_trigger(config.trigger)._temperature_c, 91.0)


class CreateNotifierTests(FactoryTestCase):
    def test_returns_a_notifier(self):
        self.assertIsInstance(create_notifier(_config()), Notifier)

    def test_is_not_started(self):
        # Lifecycle belongs to the manager that owns the notifier.
        self.assertIsNone(create_notifier(_config())._thread)

    def test_configuration_reaches_the_notifier(self):
        config = _config(Interval=45, QueueSize=7, MaxAttempts=5, RetryBackoff=2.5)
        notifier = create_notifier(config)
        self.assertEqual(notifier._name, "Discord Temperature Alerts")
        self.assertEqual(notifier._interval, 45.0)
        self.assertEqual(notifier._queue.maxsize, 7)
        self.assertEqual(notifier._max_attempts, 5)

    def test_dry_run_is_propagated(self):
        self.assertTrue(create_notifier(_config(), dry_run=True)._dry_run)
        self.assertFalse(create_notifier(_config())._dry_run)

    def test_both_example_configurations_build(self):
        for base in (_DISCORD, _HOMEASSISTANT):
            with self.subTest(endpoint=base["EndpointType"]):
                self.assertIsInstance(create_notifier(_config(base)), Notifier)


class ShippedExampleTests(LoopbackServerTestCase):
    """End to end from the file an operator actually copies.

    Parses config/notification/homeassistant.toml.example, builds the notifier
    through the factory, and delivers to a real server -- exercising the model,
    the factory, the endpoint, and the notifier together.
    """

    def test_homeassistant_example_delivers(self):
        path = (pathlib.Path(__file__).resolve().parents[2]
                / "config" / "notification" / "homeassistant.toml.example")
        with open(path, "rb") as handle:
            config = NotifierConfig.from_dict(tomllib.load(handle))

        env = {
            "FAND_HOMEASSISTANT_URL": self.origin,
            "FAND_HOMEASSISTANT_TOKEN": "ha-token-from-the-environment",
        }
        with patch.dict(os.environ, env, clear=False):
            notifier = create_notifier(config)

        notifier.deliver_now(
            Notification(
                timestamp=1_700_000_000.0,
                readings=(
                    SensorReading(name="CPU1 Temp", value_c=80.0, timestamp=0.0),
                    SensorReading(name="CPU2 Temp", value_c=75.0, timestamp=0.0),
                    SensorReading(name="n8n GPU", value_c=91.0, timestamp=0.0),
                ),
                fan_speed_percent=70.0,
                operating_mode="RUNNING",
                alarms=(),
                last_command_ok=True,
            )
        )

        paths = [r["path"] for r in self.requests]
        self.assertIn("/api/states/sensor.fand_cpu1_temp", paths)
        self.assertIn("/api/states/sensor.fand_n8n_gpu", paths)
        self.assertIn("/api/states/sensor.fand_fan_speed", paths)
        for request in self.requests:
            self.assertEqual(
                request["headers"]["authorization"],
                "Bearer ha-token-from-the-environment",
            )


if __name__ == "__main__":
    unittest.main()
