"""Tests for lib/notifications/homeassistant.py, over a real loopback server."""

from __future__ import annotations

import json
import socket
import unittest

from lib.notifications.endpoint import PermanentEndpointError, TransientEndpointError
from lib.notifications.homeassistant import HomeAssistantEndpoint
from lib.notifications.notification import Notification, SensorReading
from tests.support.http_server import LoopbackServerTestCase

_TOKEN = "eyJhbGciOiJIUzI1NiJ9.super-secret-bearer-token"


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


class HomeAssistantTestCase(LoopbackServerTestCase):
    def endpoint(self, **kwargs) -> HomeAssistantEndpoint:
        return HomeAssistantEndpoint(base_url=self.origin, token=_TOKEN, **kwargs)

    def paths(self) -> list[str]:
        return [r["path"] for r in self.requests]

    def bodies(self) -> dict[str, dict]:
        return {r["path"]: json.loads(r["body"]) for r in self.requests}


class RequestShapeTests(HomeAssistantTestCase):
    def test_one_request_per_reading_plus_fan_speed_and_mode(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0), ("CPU2 Temp", 75.0)))
        self.assertEqual(len(self.requests), 4)

    def test_entity_paths(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(
            self.paths(),
            [
                "/api/states/sensor.fand_cpu1_temp",
                "/api/states/sensor.fand_fan_speed",
                "/api/states/sensor.fand_operating_mode",
            ],
        )

    def test_sends_bearer_authorization(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        for request in self.requests:
            self.assertEqual(request["headers"]["authorization"], f"Bearer {_TOKEN}")

    def test_entity_prefix_is_configurable(self):
        self.endpoint(entity_prefix="rack1").send(_notification(("CPU1 Temp", 80.0)))
        self.assertIn("/api/states/sensor.rack1_cpu1_temp", self.paths())

    def test_base_url_trailing_slash_is_tolerated(self):
        HomeAssistantEndpoint(base_url=self.origin + "/", token=_TOKEN).send(
            _notification(("CPU1 Temp", 80.0))
        )
        self.assertIn("/api/states/sensor.fand_cpu1_temp", self.paths())

    def test_endpoint_type(self):
        self.assertEqual(self.endpoint().endpoint_type, "homeassistant")

    def test_no_readings_still_reports_fan_state_and_mode(self):
        self.endpoint().send(_notification())
        self.assertEqual(len(self.requests), 2)


class SlugTests(HomeAssistantTestCase):
    def test_slugs(self):
        cases = {
            "CPU1 Temp": "cpu1_temp",
            "Temp #2": "temp_2",
            "Inlet Temp": "inlet_temp",
            "n8n GPU": "n8n_gpu",
            "Exhaust Temp": "exhaust_temp",
            "  Odd  Name  ": "odd_name",
            "A/B|C": "a_b_c",
        }
        self.endpoint().send(_notification(*[(name, 50.0) for name in cases]))
        for name, slug in cases.items():
            with self.subTest(name=name):
                self.assertIn(f"/api/states/sensor.fand_{slug}", self.paths())

    def test_prefix_is_slugified_too(self):
        self.endpoint(entity_prefix="Rack 1!").send(_notification(("CPU1 Temp", 80.0)))
        self.assertIn("/api/states/sensor.rack_1_cpu1_temp", self.paths())

    def test_empty_prefix_falls_back(self):
        self.endpoint(entity_prefix="!!!").send(_notification(("CPU1 Temp", 80.0)))
        self.assertIn("/api/states/sensor.fand_cpu1_temp", self.paths())


class CollisionTests(HomeAssistantTestCase):
    """Two sensor names can slug to one entity id. Without a check the second
    would silently overwrite the first in Home Assistant."""

    def test_collision_warns_and_reports_only_the_first(self):
        with self.assertLogs("lib.notifications.homeassistant", level="WARNING") as logs:
            self.endpoint().send(
                _notification(("CPU1 Temp", 80.0), ("CPU1-Temp", 60.0))
            )
        self.assertIn("CPU1-Temp", logs.output[0])
        self.assertEqual(self.paths().count("/api/states/sensor.fand_cpu1_temp"), 1)
        body = self.bodies()["/api/states/sensor.fand_cpu1_temp"]
        self.assertEqual(body["state"], 80.0)

    def test_no_warning_without_a_collision(self):
        with self.assertNoLogs("lib.notifications.homeassistant", level="WARNING"):
            self.endpoint().send(
                _notification(("CPU1 Temp", 80.0), ("CPU2 Temp", 75.0))
            )


class PayloadTests(HomeAssistantTestCase):
    def test_sensor_state_and_attributes(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.44)))
        body = self.bodies()["/api/states/sensor.fand_cpu1_temp"]
        self.assertEqual(body["state"], 80.4)
        self.assertEqual(body["attributes"]["unit_of_measurement"], "°C")
        self.assertEqual(body["attributes"]["device_class"], "temperature")
        self.assertEqual(body["attributes"]["friendly_name"], "CPU1 Temp")

    def test_fan_speed_entity(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0), fan_speed_percent=72.6))
        body = self.bodies()["/api/states/sensor.fand_fan_speed"]
        self.assertEqual(body["state"], 73)
        self.assertEqual(body["attributes"]["unit_of_measurement"], "%")

    def test_unknown_fan_speed(self):
        self.endpoint().send(
            _notification(("CPU1 Temp", 80.0), fan_speed_percent=None)
        )
        self.assertEqual(
            self.bodies()["/api/states/sensor.fand_fan_speed"]["state"], "unknown",
        )

    def test_operating_mode_entity_carries_alarms(self):
        self.endpoint().send(
            _notification(
                ("CPU1 Temp", 95.0),
                operating_mode="EMERGENCY",
                alarms=("over_temperature",),
            )
        )
        body = self.bodies()["/api/states/sensor.fand_operating_mode"]
        self.assertEqual(body["state"], "EMERGENCY")
        self.assertEqual(body["attributes"]["alarms"], ["over_temperature"])


class FailureClassificationTests(HomeAssistantTestCase):
    def test_all_requests_are_attempted_despite_a_failure(self):
        # /api/states is idempotent, so attempting everything means one bad
        # entity does not cost the rest of the data.
        self.respond_to("/api/states/sensor.fand_cpu1_temp", status=503)
        with self.assertRaises(TransientEndpointError):
            self.endpoint().send(
                _notification(("CPU1 Temp", 80.0), ("CPU2 Temp", 75.0))
            )
        self.assertEqual(len(self.requests), 4)

    def test_server_error_is_transient(self):
        self.respond(status=503)
        with self.assertRaises(TransientEndpointError):
            self.endpoint().send(_notification(("CPU1 Temp", 80.0)))

    def test_unauthorized_is_permanent(self):
        self.respond(status=401)
        with self.assertRaises(PermanentEndpointError):
            self.endpoint().send(_notification(("CPU1 Temp", 80.0)))

    def test_permanent_outranks_transient(self):
        # Retrying a job containing a permanent failure can never fully
        # succeed, so the job is discarded rather than retried.
        self.respond(status=503)
        self.respond_to("/api/states/sensor.fand_cpu2_temp", status=400)
        with self.assertRaises(PermanentEndpointError):
            self.endpoint().send(
                _notification(("CPU1 Temp", 80.0), ("CPU2 Temp", 75.0))
            )

    def test_rate_limit_is_transient_with_retry_after(self):
        self.respond(status=429, headers={"Retry-After": "9"})
        with self.assertRaises(TransientEndpointError) as ctx:
            self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(ctx.exception.retry_after, 9.0)

    def test_success_raises_nothing(self):
        self.respond(status=200)
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))

    def test_transport_failure_is_transient(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        # Short timeout: this endpoint attempts every entity, so the cost is
        # paid once per request rather than once per job.
        endpoint = HomeAssistantEndpoint(
            base_url=f"http://127.0.0.1:{port}", token=_TOKEN, timeout=0.2,
        )
        with self.assertRaises(TransientEndpointError):
            endpoint.send(_notification(("CPU1 Temp", 80.0)))


class SecretContainmentTests(HomeAssistantTestCase):
    def test_token_never_appears_in_an_exception(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.respond(status=status)
                with self.assertRaises(Exception) as ctx:
                    self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
                self.assertNotIn(_TOKEN, str(ctx.exception))
                self.assertNotIn("super-secret", str(ctx.exception))

    def test_token_never_appears_in_a_log_record(self):
        with self.assertLogs("lib.notifications.homeassistant", level="WARNING") as logs:
            self.endpoint().send(_notification(("CPU1 Temp", 80.0), ("CPU1-Temp", 60.0)))
        self.assertFalse([line for line in logs.output if "super-secret" in line])

    def test_token_is_not_in_any_request_body(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        for request in self.requests:
            self.assertNotIn(_TOKEN, request["body"])


class InsecureWarningTests(HomeAssistantTestCase):
    def test_warns_when_base_url_is_not_https(self):
        with self.assertLogs("lib.utils.http", level="WARNING") as logs:
            self.endpoint()
        self.assertIn("cleartext", logs.output[0])

    def test_silent_for_https(self):
        with self.assertNoLogs("lib.utils.http", level="WARNING"):
            HomeAssistantEndpoint(base_url="https://ha.local:8123", token=_TOKEN)


if __name__ == "__main__":
    unittest.main()
