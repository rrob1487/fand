"""Typed representation of daemon-wide configuration (config.toml)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DaemonConfig:
    poll_interval: float
    log_level: str

    @classmethod
    def from_dict(cls, data: dict) -> "DaemonConfig":
        return cls(poll_interval=data["poll_interval"], log_level=data["log_level"])


@dataclass(frozen=True)
class FanCurvePoint:
    temperature_c: float
    fan_percent: float


@dataclass(frozen=True)
class FanCurveConfig:
    points: tuple[FanCurvePoint, ...]
    hysteresis_percent: float = 5.0

    @classmethod
    def from_dict(cls, data: dict) -> "FanCurveConfig":
        points = tuple(
            FanCurvePoint(temperature_c=point[0], fan_percent=point[1])
            for point in data.get("points", [])
        )
        return cls(points=points, hysteresis_percent=data.get("hysteresis_percent", 5.0))


@dataclass(frozen=True)
class SafetyConfig:
    max_temperature: float
    shutdown_on_emergency: bool = False
    recovery_margin_c: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> "SafetyConfig":
        return cls(
            max_temperature=data["max_temperature"],
            shutdown_on_emergency=data.get("shutdown_on_emergency", False),
            recovery_margin_c=data.get("recovery_margin_c", 0.0),
        )


@dataclass(frozen=True)
class WatchdogConfig:
    enabled: bool

    @classmethod
    def from_dict(cls, data: dict) -> "WatchdogConfig":
        return cls(enabled=data["enabled"])


@dataclass(frozen=True)
class Config:
    daemon: DaemonConfig
    fan_curve: FanCurveConfig
    safety: SafetyConfig
    watchdog: WatchdogConfig

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        return cls(
            daemon=DaemonConfig.from_dict(data["daemon"]),
            fan_curve=FanCurveConfig.from_dict(data["fan_curve"]),
            safety=SafetyConfig.from_dict(data["safety"]),
            watchdog=WatchdogConfig.from_dict(data["watchdog"]),
        )
