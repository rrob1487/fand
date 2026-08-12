"""Tests for lib/notifications/discord.py, over a real loopback server."""

from __future__ import annotations

import socket
import unittest

from lib.notifications.discord import DiscordEndpoint
from lib.notifications.endpoint import PermanentEndpointError, TransientEndpointError
from lib.notifications.notification import Notification, SensorReading
from tests.support.http_server import LoopbackServerTestCase

_TOKEN = "MTk4NzY1NDMyMTA5.Gh3Kx9.super-secret-bot-token"
_CHANNEL = "112233445566778899"


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


class DiscordTestCase(LoopbackServerTestCase):
    def endpoint(self, **kwargs) -> DiscordEndpoint:
        return DiscordEndpoint(
            token=_TOKEN, channel_id=_CHANNEL, base_url=self.origin, **kwargs,
        )

    def embed(self) -> dict:
        return self.last_json()["embeds"][0]


class RequestShapeTests(DiscordTestCase):
    def test_posts_to_the_channel_messages_endpoint(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(
            self.last_request["path"], f"/api/v10/channels/{_CHANNEL}/messages",
        )

    def test_sends_bot_authorization(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(
            self.last_request["headers"]["authorization"], f"Bot {_TOKEN}",
        )

    def test_sends_exactly_one_embed(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(len(self.last_json()["embeds"]), 1)

    def test_base_url_trailing_slash_is_tolerated(self):
        DiscordEndpoint(
            token=_TOKEN, channel_id=_CHANNEL, base_url=self.origin + "/",
        ).send(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(
            self.last_request["path"], f"/api/v10/channels/{_CHANNEL}/messages",
        )

    def test_endpoint_type(self):
        self.assertEqual(self.endpoint().endpoint_type, "discord")


class EmbedContentTests(DiscordTestCase):
    def test_colour_tracks_operating_mode(self):
        for mode, colour in (
            ("RUNNING", 0x2ECC71),
            ("WARNING", 0xF39C12),
            ("EMERGENCY", 0xE74C3C),
            ("STARTING", 0x95A5A6),
        ):
            with self.subTest(mode=mode):
                self.endpoint().send(
                    _notification(("CPU1 Temp", 80.0), operating_mode=mode)
                )
                self.assertEqual(self.embed()["color"], colour)

    def test_unknown_mode_gets_the_default_colour(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0), operating_mode="WAT"))
        self.assertEqual(self.embed()["color"], 0x95A5A6)

    def test_title_carries_the_mode(self):
        self.endpoint().send(
            _notification(("CPU1 Temp", 80.0), operating_mode="EMERGENCY")
        )
        self.assertIn("EMERGENCY", self.embed()["title"])

    def test_hottest_sensor_is_the_headline(self):
        self.endpoint().send(
            _notification(("CPU1 Temp", 80.0), ("n8n GPU", 91.0), ("CPU2 Temp", 75.0))
        )
        self.assertIn("n8n GPU", self.embed()["description"])
        self.assertIn("91.0", self.embed()["description"])

    def test_no_description_without_readings(self):
        self.endpoint().send(_notification())
        self.assertNotIn("description", self.embed())

    def test_fan_speed_and_mode_fields(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        fields = {f["name"]: f["value"] for f in self.embed()["fields"]}
        self.assertEqual(fields["Fan speed"], "70%")
        self.assertEqual(fields["Mode"], "RUNNING")

    def test_unknown_fan_speed(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0), fan_speed_percent=None))
        fields = {f["name"]: f["value"] for f in self.embed()["fields"]}
        self.assertEqual(fields["Fan speed"], "unknown")

    def test_sensor_block_lists_every_reading(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0), ("CPU2 Temp", 75.5)))
        fields = {f["name"]: f["value"] for f in self.embed()["fields"]}
        self.assertIn("CPU1 Temp", fields["Sensors"])
        self.assertIn("75.5", fields["Sensors"])

    def test_no_sensor_field_without_readings(self):
        self.endpoint().send(_notification())
        names = [f["name"] for f in self.embed()["fields"]]
        self.assertNotIn("Sensors", names)

    def test_alarms_field_only_when_present(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertNotIn("Alarms", [f["name"] for f in self.embed()["fields"]])

        self.endpoint().send(
            _notification(("CPU1 Temp", 95.0), alarms=("over_temperature",))
        )
        fields = {f["name"]: f["value"] for f in self.embed()["fields"]}
        self.assertIn("over_temperature", fields["Alarms"])

    def test_timestamp_is_iso8601(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertTrue(self.embed()["timestamp"].startswith("2023-11-14T"))


class FieldLimitTests(DiscordTestCase):
    """Discord rejects a field value over 1024 characters with a 400, which is
    classified permanent — so an over-long sensor list would silently discard
    every notification from a machine with enough sensors."""

    def test_long_sensor_list_is_truncated(self):
        readings = tuple((f"Some Rather Long Sensor Name {i}", 50.0 + i) for i in range(80))
        self.endpoint().send(_notification(*readings))
        fields = {f["name"]: f["value"] for f in self.embed()["fields"]}
        self.assertLessEqual(len(fields["Sensors"]), 1024)
        self.assertIn("more", fields["Sensors"])

    def test_every_embed_field_stays_within_the_limit(self):
        readings = tuple((f"Sensor {i}", 50.0) for i in range(200))
        self.endpoint().send(_notification(*readings))
        for field in self.embed()["fields"]:
            with self.subTest(field=field["name"]):
                self.assertLessEqual(len(field["value"]), 1024)

    def test_short_list_is_not_truncated(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0), ("CPU2 Temp", 75.0)))
        fields = {f["name"]: f["value"] for f in self.embed()["fields"]}
        self.assertNotIn("more", fields["Sensors"])


class FailureClassificationTests(DiscordTestCase):
    def test_rate_limit_is_transient_with_retry_after(self):
        self.respond(status=429, headers={"Retry-After": "7"})
        with self.assertRaises(TransientEndpointError) as ctx:
            self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(ctx.exception.retry_after, 7.0)

    def test_server_error_is_transient(self):
        self.respond(status=503)
        with self.assertRaises(TransientEndpointError):
            self.endpoint().send(_notification(("CPU1 Temp", 80.0)))

    def test_unauthorized_is_permanent(self):
        self.respond(status=401, body='{"message":"401: Unauthorized"}')
        with self.assertRaises(PermanentEndpointError):
            self.endpoint().send(_notification(("CPU1 Temp", 80.0)))

    def test_forbidden_is_permanent(self):
        self.respond(status=403)
        with self.assertRaises(PermanentEndpointError):
            self.endpoint().send(_notification(("CPU1 Temp", 80.0)))

    def test_success_raises_nothing(self):
        self.respond(status=200, body='{"id":"1"}')
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))

    def test_transport_failure_is_transient(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        endpoint = DiscordEndpoint(
            token=_TOKEN, channel_id=_CHANNEL,
            base_url=f"http://127.0.0.1:{port}", timeout=0.5,
        )
        with self.assertRaises(TransientEndpointError):
            endpoint.send(_notification(("CPU1 Temp", 80.0)))


class SecretContainmentTests(DiscordTestCase):
    def test_token_never_appears_in_an_exception(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.respond(status=status)
                with self.assertRaises(Exception) as ctx:
                    self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
                self.assertNotIn(_TOKEN, str(ctx.exception))
                self.assertNotIn("super-secret", str(ctx.exception))

    def test_token_never_appears_in_a_log_record(self):
        with self.assertLogs("lib.notifications", level="DEBUG") as logs:
            readings = tuple((f"Sensor {i}", 50.0) for i in range(200))
            self.endpoint().send(_notification(*readings))
        self.assertFalse([line for line in logs.output if "super-secret" in line])

    def test_token_is_not_in_the_message_body(self):
        self.endpoint().send(_notification(("CPU1 Temp", 80.0)))
        self.assertNotIn(_TOKEN, self.last_request["body"])


class InsecureWarningTests(DiscordTestCase):
    def test_warns_when_base_url_is_not_https(self):
        with self.assertLogs("lib.utils.http", level="WARNING") as logs:
            self.endpoint()
        self.assertIn("cleartext", logs.output[0])

    def test_silent_for_the_default_https_base(self):
        with self.assertNoLogs("lib.utils.http", level="WARNING"):
            DiscordEndpoint(token=_TOKEN, channel_id=_CHANNEL)


if __name__ == "__main__":
    unittest.main()
