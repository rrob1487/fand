"""Typed representation of a notifier's configuration (config/notification/*.toml).

Holds environment variable *names* rather than resolved secrets, so a credential
cannot leak through a repr, a dataclass string conversion, or a traceback.
Resolution happens in the factory; see docs/notification.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

# A [Credentials] value must look like an environment variable name. Anything
# else is most likely a secret pasted where a reference belongs.
_ENV_VAR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Bounds queue memory when a configuration asks for something unreasonable.
_MAX_QUEUE_SIZE = 10_000

_DEFAULT_ENABLED = True
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_BACKOFF_SECONDS = 1.0

_EMPTY_OPTIONS: Mapping[str, Any] = MappingProxyType({})


class NotifierConfigError(Exception):
    """Raised when a notifier configuration is missing a required value or
    contains one that cannot be used.

    A single exception type so the loader has one thing to catch when it logs
    and skips an invalid notifier. `KeyError` alone cannot express a rule like
    "Interval must be greater than zero".
    """


# --------------------------------------------------------------------------
# Validation helpers
#
# Each takes a display name used in error messages, so a failure points at the
# offending line of TOML rather than at a Python attribute.
# --------------------------------------------------------------------------
def _require(data: Mapping[str, Any], key: str, display: str | None = None) -> Any:
    try:
        return data[key]
    except KeyError:
        raise NotifierConfigError(f"{display or key} is required") from None


def _string(value: Any, display: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NotifierConfigError(f"{display} must be a non-empty string")
    return value


def _boolean(value: Any, display: str) -> bool:
    if not isinstance(value, bool):
        raise NotifierConfigError(f"{display} must be true or false")
    return value


def _number(
    value: Any, display: str, *, minimum: float | None = None, exclusive: bool = False,
) -> float:
    # bool is a subclass of int, so `Interval = true` would otherwise pass.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise NotifierConfigError(f"{display} must be a number")
    if minimum is not None:
        if exclusive and value <= minimum:
            raise NotifierConfigError(f"{display} must be greater than {minimum}")
        if not exclusive and value < minimum:
            raise NotifierConfigError(f"{display} must be at least {minimum}")
    return float(value)


def _integer(
    value: Any, display: str, *, minimum: int, maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NotifierConfigError(f"{display} must be a whole number")
    if value < minimum:
        raise NotifierConfigError(f"{display} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise NotifierConfigError(f"{display} must be at most {maximum}")
    return value


def _table(value: Any, display: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise NotifierConfigError(f"{display} must be a table")
    return value


def _reject_unknown(data: Mapping[str, Any], known: frozenset[str], scope: str = "") -> None:
    """Fail on keys this model does not recognize.

    A silently ignored key means a notifier quietly behaves differently from
    what its file says — a mistyped optional key would otherwise fall back to
    its default with nothing logged.
    """
    unknown = sorted(set(data) - known)
    if not unknown:
        return
    prefix = f"{scope} " if scope else ""
    plural = "s" if len(unknown) > 1 else ""
    raise NotifierConfigError(f"{prefix}unknown key{plural}: {', '.join(unknown)}")


def _parse_sensors(data: Mapping[str, Any]) -> tuple[str, ...] | None:
    """Sensor selection. Omitted means every available sensor."""
    if "Sensors" not in data:
        return None
    raw = data["Sensors"]
    if not isinstance(raw, list):
        raise NotifierConfigError("[Trigger].Sensors must be a list of sensor names")
    if not raw:
        raise NotifierConfigError(
            "[Trigger].Sensors is empty; omit it to select every available sensor"
        )
    return tuple(_string(name, "[Trigger].Sensors entry") for name in raw)


# --------------------------------------------------------------------------
# Trigger configuration
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TriggerConfig:
    """When a notifier generates notification jobs.

    `sensors` scopes both trigger evaluation and the notification payload;
    None means every available sensor. It deliberately has no default — a
    default here would make subclass fields illegal, since a non-default
    field cannot follow a defaulted one.
    """

    sensors: tuple[str, ...] | None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TriggerConfig":
        raise NotImplementedError


@dataclass(frozen=True)
class ThresholdTriggerConfig(TriggerConfig):
    """Active while the hottest selected sensor is at or above a temperature."""

    temperature_c: float

    _KNOWN = frozenset({"Type", "Temperature", "Sensors"})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ThresholdTriggerConfig":
        _reject_unknown(data, cls._KNOWN, "[Trigger]")
        return cls(
            sensors=_parse_sensors(data),
            temperature_c=_number(
                _require(data, "Temperature", "[Trigger].Temperature"),
                "[Trigger].Temperature",
                # A negative threshold would be satisfied by every reading a
                # server sensor can produce, making the notifier permanently
                # active — that is a general trigger written as a threshold.
                minimum=0,
            ),
        )


@dataclass(frozen=True)
class GeneralTriggerConfig(TriggerConfig):
    """Always active; fires purely on the notifier's interval.

    Carries no extra fields, but exists as its own type so construction stays
    a registry lookup rather than a conditional.
    """

    _KNOWN = frozenset({"Type", "Sensors"})

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GeneralTriggerConfig":
        # Temperature is absent from _KNOWN, so a "general" trigger carrying
        # one is rejected — that is a threshold notifier with the wrong Type.
        _reject_unknown(data, cls._KNOWN, "[Trigger]")
        return cls(sensors=_parse_sensors(data))


# Adding a trigger type is a new class plus one entry here.
_TRIGGER_TYPES: Mapping[str, type[TriggerConfig]] = MappingProxyType(
    {
        "threshold": ThresholdTriggerConfig,
        "general": GeneralTriggerConfig,
    }
)


def _parse_trigger(data: Mapping[str, Any]) -> TriggerConfig:
    table = _table(_require(data, "Trigger", "[Trigger]"), "[Trigger]")
    trigger_type = _string(_require(table, "Type", "[Trigger].Type"), "[Trigger].Type")
    try:
        trigger_cls = _TRIGGER_TYPES[trigger_type]
    except KeyError:
        raise NotifierConfigError(
            f"[Trigger].Type {trigger_type!r} is not one of: "
            f"{', '.join(sorted(_TRIGGER_TYPES))}"
        ) from None
    return trigger_cls.from_dict(table)


def _parse_credentials(data: Mapping[str, Any]) -> Mapping[str, str]:
    """Endpoint credentials as environment variable names.

    The endpoint decides what the keys mean; this only enforces that every
    value is a reference rather than a secret.
    """
    table = _table(_require(data, "Credentials", "[Credentials]"), "[Credentials]")
    if not table:
        raise NotifierConfigError("[Credentials] must name at least one environment variable")

    names: dict[str, str] = {}
    for key, value in table.items():
        if not isinstance(value, str) or not _ENV_VAR_NAME.match(value):
            # The value is never included in this message: a value that fails
            # the check is most likely the secret itself, and this text goes
            # straight to the log.
            raise NotifierConfigError(
                f"[Credentials].{key} must be an environment variable name, not a value"
            )
        names[key] = value
    return MappingProxyType(names)


def _parse_endpoint_options(data: Mapping[str, Any]) -> Mapping[str, Any]:
    """Non-secret endpoint options, passed through without interpretation.

    Contents belong to the endpoint implementation, so keys are not validated
    here — only that the table is a table.
    """
    if "Endpoint" not in data:
        return _EMPTY_OPTIONS
    return MappingProxyType(dict(_table(data["Endpoint"], "[Endpoint]")))


# --------------------------------------------------------------------------
# Notifier configuration
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class NotifierConfig:
    """One notifier, parsed from one config/notification/*.toml file.

    Carries no identity of its own: a notifier is identified by its file path,
    which the loader keeps as the key of its mapping. That keeps equality a
    pure comparison of content, which is what lets a reload leave an unchanged
    notifier's worker and queue running.
    """

    name: str
    endpoint_type: str
    enabled: bool
    interval_seconds: float
    queue_size: int
    max_attempts: int
    retry_backoff_seconds: float
    trigger: TriggerConfig
    credentials: Mapping[str, str]
    endpoint_options: Mapping[str, Any]

    _KNOWN = frozenset(
        {
            "Name",
            "EndpointType",
            "Enabled",
            "Interval",
            "QueueSize",
            "MaxAttempts",
            "RetryBackoff",
            "Trigger",
            "Endpoint",
            "Credentials",
        }
    )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "NotifierConfig":
        _reject_unknown(data, cls._KNOWN)
        return cls(
            name=_string(_require(data, "Name"), "Name"),
            # Only checked as a string: which endpoint types exist is the
            # factory's knowledge, not the model's.
            endpoint_type=_string(_require(data, "EndpointType"), "EndpointType"),
            enabled=(
                _boolean(data["Enabled"], "Enabled")
                if "Enabled" in data
                else _DEFAULT_ENABLED
            ),
            interval_seconds=_number(
                _require(data, "Interval"), "Interval", minimum=0, exclusive=True,
            ),
            queue_size=_integer(
                _require(data, "QueueSize"), "QueueSize",
                minimum=1, maximum=_MAX_QUEUE_SIZE,
            ),
            max_attempts=(
                _integer(data["MaxAttempts"], "MaxAttempts", minimum=1)
                if "MaxAttempts" in data
                else _DEFAULT_MAX_ATTEMPTS
            ),
            retry_backoff_seconds=(
                _number(data["RetryBackoff"], "RetryBackoff", minimum=0)
                if "RetryBackoff" in data
                else _DEFAULT_RETRY_BACKOFF_SECONDS
            ),
            trigger=_parse_trigger(data),
            credentials=_parse_credentials(data),
            endpoint_options=_parse_endpoint_options(data),
        )
