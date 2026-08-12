"""Discord endpoint: delivers a notification as a channel message.

Uses the bot REST API, per notification.md's credential schema of token,
server, and channel. Messages are embeds colour-coded by operating mode, since
this is a human-facing service where a thermal alert has to be readable at a
glance rather than complete.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lib.notifications.endpoint import (
    NotificationEndpoint,
    TransientEndpointError,
    raise_for_http_status,
)
from lib.notifications.notification import Notification
from lib.utils.http import HTTPTransportError, post_json, warn_if_insecure
from lib.utils.logging import get_logger

_log = get_logger(__name__)

_API_VERSION = "v10"

# Discord rejects a field value over 1024 characters with a 400, which the
# status classifier would treat as permanent — every notification silently
# discarded once a machine reports enough sensors. The block is truncated
# below this instead.
_MAX_FIELD_CHARS = 1024
_TRUNCATION_MARGIN = 64

_MODE_COLOURS = {
    "STARTING": 0x95A5A6,   # grey
    "RUNNING": 0x2ECC71,    # green
    "WARNING": 0xF39C12,    # amber
    "EMERGENCY": 0xE74C3C,  # red
}
_DEFAULT_COLOUR = 0x95A5A6


class DiscordEndpoint(NotificationEndpoint):
    """Posts to one Discord channel as a bot.

    Credentials arrive already resolved; this class never reads the
    environment and never logs the token.
    """

    def __init__(
        self,
        token: str,
        channel_id: str,
        server_id: str | None = None,
        base_url: str = "https://discord.com",
        timeout: float = 10.0,
    ) -> None:
        self._token = token
        self._channel_id = channel_id
        # Not used by the REST call, which addresses the channel directly.
        # Kept because notification.md specifies it, and it identifies which
        # guild a notifier targets in diagnostics.
        self._server_id = server_id
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        warn_if_insecure(self._base_url)

    @property
    def endpoint_type(self) -> str:
        return "discord"

    def send(self, notification: Notification) -> None:
        url = f"{self._base_url}/api/{_API_VERSION}/channels/{self._channel_id}/messages"
        headers = {"Authorization": f"Bot {self._token}"}
        try:
            response = post_json(
                url, self._build_payload(notification), headers, timeout=self._timeout,
            )
        except HTTPTransportError as exc:
            # Never reached the server; another attempt may.
            raise TransientEndpointError(f"discord: {exc}") from exc
        raise_for_http_status(response.status, response.retry_after, "discord")

    # ----------------------------------------------------------------------
    # Payload construction
    # ----------------------------------------------------------------------
    def _build_payload(self, notification: Notification) -> dict:
        return {"embeds": [self._build_embed(notification)]}

    def _build_embed(self, notification: Notification) -> dict:
        embed: dict = {
            "title": f"fand — {notification.operating_mode}",
            "color": _MODE_COLOURS.get(notification.operating_mode, _DEFAULT_COLOUR),
            "timestamp": _iso8601(notification.timestamp),
            "fields": [],
        }

        hottest = notification.hottest
        if hottest is not None:
            embed["description"] = (
                f"Hottest: **{hottest.name}** {hottest.value_c:.1f} °C"
            )

        embed["fields"].append(
            {
                "name": "Fan speed",
                "value": _format_fan_speed(notification.fan_speed_percent),
                "inline": True,
            }
        )
        embed["fields"].append(
            {"name": "Mode", "value": notification.operating_mode, "inline": True}
        )

        if notification.readings:
            embed["fields"].append(
                {"name": "Sensors", "value": _format_sensors(notification.readings)}
            )
        if notification.alarms:
            embed["fields"].append(
                {"name": "Alarms", "value": ", ".join(notification.alarms)}
            )
        return embed


def _iso8601(timestamp: float) -> str:
    """Discord renders this in each viewer's local time."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _format_fan_speed(percent: float | None) -> str:
    return "unknown" if percent is None else f"{percent:.0f}%"


def _format_sensors(readings: tuple) -> str:
    """A code block of readings, bounded to Discord's field limit."""
    width = max(len(reading.name) for reading in readings)
    lines = [f"{r.name:<{width}}  {r.value_c:>6.1f} °C" for r in readings]

    body = "\n".join(lines)
    if len(body) + _TRUNCATION_MARGIN <= _MAX_FIELD_CHARS:
        return f"```\n{body}\n```"

    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if used + len(line) + _TRUNCATION_MARGIN > _MAX_FIELD_CHARS:
            kept.append(f"... +{len(lines) - index} more")
            _log.debug(
                "truncated Discord sensor list to %d of %d rows", index, len(lines),
            )
            break
        kept.append(line)
        used += len(line) + 1
    return "```\n" + "\n".join(kept) + "\n```"
