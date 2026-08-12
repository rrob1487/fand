"""Creates runtime Notifiers from validated notifier configuration.

This is the only place that turns configuration into objects: it resolves
credential references against the environment, checks that an endpoint got the
keys it needs, and hands the results to constructors that take plain values.
That is what lets lib/notifications/ stay free of any knowledge about
configuration files or the environment.

Adding an endpoint type means writing its module under lib/notifications/,
adding one entry to ENDPOINT_BUILDERS, and shipping a configuration file. No
manager, controller, daemon, or fan-control code changes.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Mapping

from lib.models.notification import (
    GeneralTriggerConfig,
    NotifierConfig,
    NotifierConfigError,
    ThresholdTriggerConfig,
    TriggerConfig,
)
from lib.notifications.discord import DiscordEndpoint
from lib.notifications.endpoint import NotificationEndpoint
from lib.notifications.homeassistant import HomeAssistantEndpoint
from lib.notifications.notifier import Notifier
from lib.notifications.trigger import GeneralTrigger, ThresholdTrigger, Trigger


# --------------------------------------------------------------------------
# Value coercion for [Endpoint] options
# --------------------------------------------------------------------------
def _positive_number(value: Any, display: str) -> float:
    # bool is a subclass of int, so `Timeout = true` would otherwise pass.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NotifierConfigError(f"{display} must be a number")
    if value <= 0:
        raise NotifierConfigError(f"{display} must be greater than 0")
    return float(value)


def _text(value: Any, display: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotifierConfigError(f"{display} must be a non-empty string")
    return value


# --------------------------------------------------------------------------
# Credentials and options
# --------------------------------------------------------------------------
def _resolve_credentials(
    credentials: Mapping[str, str],
    endpoint_type: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Look up each configured credential reference in the environment.

    Configuration holds variable *names*; the values live in the environment.
    Every message here names the key or the variable, never the value, because
    these messages are written to the log.
    """
    unknown = sorted(set(credentials) - required - optional)
    if unknown:
        raise NotifierConfigError(
            f"endpoint type {endpoint_type!r} does not use "
            f"[Credentials] {', '.join(unknown)}"
        )
    missing = sorted(required - set(credentials))
    if missing:
        raise NotifierConfigError(
            f"endpoint type {endpoint_type!r} requires "
            f"[Credentials] {', '.join(missing)}"
        )

    resolved: dict[str, str] = {}
    for key, variable in credentials.items():
        value = os.environ.get(variable, "")
        if not value.strip():
            raise NotifierConfigError(
                f"[Credentials].{key} refers to environment variable "
                f"{variable}, which is unset or empty"
            )
        resolved[key] = value
    return resolved


def _resolve_options(
    options: Mapping[str, Any],
    endpoint_type: str,
    spec: Mapping[str, tuple[str, Callable[[Any, str], Any]]],
) -> dict[str, Any]:
    """Validate [Endpoint] options and map them to constructor keywords.

    The model passes this table through without interpretation because the
    schema belongs to the endpoint. That makes the factory the first thing
    able to catch a typo here, so unknown keys are rejected rather than
    silently leaving a default in place.
    """
    unknown = sorted(set(options) - set(spec))
    if unknown:
        raise NotifierConfigError(
            f"endpoint type {endpoint_type!r} does not use "
            f"[Endpoint] {', '.join(unknown)}"
        )
    resolved: dict[str, Any] = {}
    for key, value in options.items():
        keyword, coerce = spec[key]
        resolved[keyword] = coerce(value, f"[Endpoint].{key}")
    return resolved


# --------------------------------------------------------------------------
# Endpoint builders
#
# One per endpoint type, because each service needs different credentials and
# options. Keeping construction in a function rather than a declarative table
# means an endpoint with an unusual requirement does not force the registry to
# grow a new concept.
# --------------------------------------------------------------------------
def _build_discord(
    credentials: Mapping[str, str], options: Mapping[str, Any],
) -> NotificationEndpoint:
    resolved = _resolve_credentials(
        credentials, "discord",
        required=frozenset({"Token", "Channel"}),
        # Server identifies the guild in diagnostics; the REST call addresses
        # the channel directly, so it is accepted but not required.
        optional=frozenset({"Server"}),
    )
    return DiscordEndpoint(
        token=resolved["Token"],
        channel_id=resolved["Channel"],
        server_id=resolved.get("Server"),
        **_resolve_options(
            options, "discord", {"Timeout": ("timeout", _positive_number)},
        ),
    )


def _build_homeassistant(
    credentials: Mapping[str, str], options: Mapping[str, Any],
) -> NotificationEndpoint:
    resolved = _resolve_credentials(
        credentials, "homeassistant", required=frozenset({"URL", "Token"}),
    )
    return HomeAssistantEndpoint(
        base_url=resolved["URL"],
        token=resolved["Token"],
        **_resolve_options(
            options, "homeassistant",
            {
                "Timeout": ("timeout", _positive_number),
                "EntityPrefix": ("entity_prefix", _text),
            },
        ),
    )


#: The endpoint registry. One entry per supported EndpointType.
ENDPOINT_BUILDERS: dict[
    str, Callable[[Mapping[str, str], Mapping[str, Any]], NotificationEndpoint]
] = {
    "discord": _build_discord,
    "homeassistant": _build_homeassistant,
}


#: Trigger construction, keyed by the configuration type the model produced.
#: Keyed by class rather than by a type string so the string appears in exactly
#: one place: the model that parses it.
_TRIGGER_BUILDERS: dict[type[TriggerConfig], Callable[[Any], Trigger]] = {
    ThresholdTriggerConfig: lambda cfg: ThresholdTrigger(cfg.sensors, cfg.temperature_c),
    GeneralTriggerConfig: lambda cfg: GeneralTrigger(cfg.sensors),
}


def create_endpoint(config: NotifierConfig) -> NotificationEndpoint:
    """Build the endpoint a notifier configuration names.

    Membership of EndpointType is checked here rather than in the model,
    because the registry is what defines which types exist.
    """
    try:
        build = ENDPOINT_BUILDERS[config.endpoint_type]
    except KeyError:
        raise NotifierConfigError(
            f"EndpointType {config.endpoint_type!r} is not one of: "
            f"{', '.join(sorted(ENDPOINT_BUILDERS))}"
        ) from None
    return build(config.credentials, config.endpoint_options)


def create_trigger(config: TriggerConfig) -> Trigger:
    try:
        build = _TRIGGER_BUILDERS[type(config)]
    except KeyError:  # pragma: no cover - the model cannot produce another
        raise NotifierConfigError(
            f"unsupported trigger configuration: {type(config).__name__}"
        ) from None
    return build(config)


def create_notifier(config: NotifierConfig, dry_run: bool = False) -> Notifier:
    """Build a Notifier from its configuration.

    Returned stopped: starting a worker is lifecycle, which belongs to the
    manager that owns the notifier.
    """
    return Notifier(
        name=config.name,
        endpoint=create_endpoint(config),
        trigger=create_trigger(config.trigger),
        interval_seconds=config.interval_seconds,
        queue_size=config.queue_size,
        max_attempts=config.max_attempts,
        retry_backoff_seconds=config.retry_backoff_seconds,
        dry_run=dry_run,
    )
