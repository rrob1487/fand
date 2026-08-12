"""Home Assistant endpoint: delivers a notification as entity states.

One POST to /api/states per included sensor, plus one for the requested fan
speed and one for the operating mode, so the notification's sensor data, fan
state, and system state all become entities. This is a monitoring destination,
so completeness matters more than brevity.
"""

from __future__ import annotations

import re

from lib.notifications.endpoint import (
    EndpointError,
    NotificationEndpoint,
    PermanentEndpointError,
    TransientEndpointError,
    raise_for_http_status,
)
from lib.notifications.notification import Notification
from lib.utils.http import HTTPTransportError, post_json, warn_if_insecure
from lib.utils.logging import get_logger

_log = get_logger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


class HomeAssistantEndpoint(NotificationEndpoint):
    """Writes fand's readings into Home Assistant as sensor entities.

    Credentials arrive already resolved; this class never reads the
    environment and never logs the token.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        entity_prefix: str = "fand",
        timeout: float = 10.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._prefix = _slugify(entity_prefix) or "fand"
        self._timeout = timeout
        warn_if_insecure(self._base_url)

    @property
    def endpoint_type(self) -> str:
        return "homeassistant"

    def send(self, notification: Notification) -> None:
        """Write every entity, then raise the worst failure seen.

        Requests are not stopped at the first failure: /api/states is
        idempotent, so re-sending on a retry is harmless, and attempting all of
        them means a single bad entity does not cost the rest of the data.
        A permanent failure outranks a transient one, since retrying a job that
        contains one can never fully succeed.
        """
        transient: EndpointError | None = None
        permanent: EndpointError | None = None

        for entity_id, payload in self._entities(notification):
            try:
                self._put_state(entity_id, payload)
            except PermanentEndpointError as exc:
                permanent = permanent or exc
            except TransientEndpointError as exc:
                transient = transient or exc

        if permanent is not None:
            raise permanent
        if transient is not None:
            raise transient

    # ----------------------------------------------------------------------
    def _put_state(self, entity_id: str, payload: dict) -> None:
        url = f"{self._base_url}/api/states/{entity_id}"
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            response = post_json(url, payload, headers, timeout=self._timeout)
        except HTTPTransportError as exc:
            raise TransientEndpointError(f"homeassistant: {exc}") from exc
        raise_for_http_status(
            response.status, response.retry_after, f"homeassistant {entity_id}",
        )

    def _entities(self, notification: Notification) -> list[tuple[str, dict]]:
        entities: list[tuple[str, dict]] = []
        seen: dict[str, str] = {}

        for reading in notification.readings:
            entity_id = f"sensor.{self._prefix}_{_slugify(reading.name)}"
            if entity_id in seen:
                # Two sensor names can slug to one entity ("CPU1 Temp" and
                # "CPU1-Temp"). Without this the second would overwrite the
                # first in Home Assistant with nothing logged.
                _log.warning(
                    "sensors %r and %r both map to %s; reporting only the first",
                    seen[entity_id], reading.name, entity_id,
                )
                continue
            seen[entity_id] = reading.name
            entities.append(
                (
                    entity_id,
                    {
                        "state": round(reading.value_c, 1),
                        "attributes": {
                            "unit_of_measurement": "°C",
                            "device_class": "temperature",
                            "friendly_name": reading.name,
                        },
                    },
                )
            )

        entities.append(
            (
                f"sensor.{self._prefix}_fan_speed",
                {
                    "state": (
                        "unknown"
                        if notification.fan_speed_percent is None
                        else round(notification.fan_speed_percent)
                    ),
                    "attributes": {
                        "unit_of_measurement": "%",
                        "friendly_name": "fand fan speed",
                    },
                },
            )
        )
        entities.append(
            (
                f"sensor.{self._prefix}_operating_mode",
                {
                    "state": notification.operating_mode,
                    "attributes": {
                        "friendly_name": "fand operating mode",
                        "alarms": list(notification.alarms),
                    },
                },
            )
        )
        return entities


def _slugify(name: str) -> str:
    """Home Assistant object ids allow lowercase alphanumerics and underscores.

    "CPU1 Temp" -> "cpu1_temp", "Temp #2" -> "temp_2".
    """
    return _NON_ALNUM.sub("_", name.lower()).strip("_")
