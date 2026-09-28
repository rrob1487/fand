"""Runtime state: the current truth of the system. Data only — see policy.py
for decisions derived from this data.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class OperatingMode(Enum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WARNING = "WARNING"
    EMERGENCY = "EMERGENCY"
    # A sensor that was reading fine has been lost long enough that the daemon
    # can no longer claim to see the whole machine, so fan control is handed
    # back to iDRAC until it returns.
    FAILSAFE = "FAILSAFE"


@dataclass
class TemperatureReading:
    value: float
    timestamp: float


@dataclass
class HardwareCommandResult:
    success: bool
    detail: str
    timestamp: float


class State:
    """Mutable snapshot of the system. Plain setters only — no evaluation."""

    def __init__(self) -> None:
        self.temperatures: dict[str, TemperatureReading] = {}
        self.alarms: set[str] = set()
        self.mode: OperatingMode = OperatingMode.STARTING
        # None both before the first decision and while iDRAC has the fans.
        self.requested_fan_speed: float | None = None
        self.last_command_result: HardwareCommandResult | None = None
        # Previously-good sensors that are currently failing, mapped to the
        # time.monotonic() at which they were first lost. Written by
        # SensorManager; Policy decides how long is too long.
        self.lost_sensors: dict[str, float] = {}
        # The subset of lost_sensors Policy judged lost for too long, sorted.
        self.unmonitored_sensors: tuple[str, ...] = ()

    def update_temperature(self, sensor_name: str, value: float) -> None:
        self.temperatures[sensor_name] = TemperatureReading(value=value, timestamp=time.time())

    def clear_temperature(self, sensor_name: str) -> None:
        self.temperatures.pop(sensor_name, None)

    def mark_sensor_lost(self, sensor_name: str) -> None:
        # setdefault: the loss began at the first failure, not the latest one,
        # or the grace period would restart every poll and never expire.
        self.lost_sensors.setdefault(sensor_name, time.monotonic())

    def clear_sensor_lost(self, sensor_name: str) -> None:
        self.lost_sensors.pop(sensor_name, None)

    def set_unmonitored_sensors(self, sensor_names: tuple[str, ...]) -> None:
        self.unmonitored_sensors = sensor_names

    def set_alarm(self, name: str) -> None:
        self.alarms.add(name)

    def clear_alarm(self, name: str) -> None:
        self.alarms.discard(name)

    def set_mode(self, mode: OperatingMode) -> None:
        self.mode = mode

    def set_requested_fan_speed(self, percent: float | None) -> None:
        self.requested_fan_speed = percent

    def set_last_command_result(self, success: bool, detail: str = "") -> None:
        self.last_command_result = HardwareCommandResult(
            success=success, detail=detail, timestamp=time.time(),
        )
