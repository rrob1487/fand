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
        self.requested_fan_speed: float | None = None
        self.last_command_result: HardwareCommandResult | None = None

    def update_temperature(self, sensor_name: str, value: float) -> None:
        self.temperatures[sensor_name] = TemperatureReading(value=value, timestamp=time.time())

    def clear_temperature(self, sensor_name: str) -> None:
        self.temperatures.pop(sensor_name, None)

    def set_alarm(self, name: str) -> None:
        self.alarms.add(name)

    def clear_alarm(self, name: str) -> None:
        self.alarms.discard(name)

    def set_mode(self, mode: OperatingMode) -> None:
        self.mode = mode

    def set_requested_fan_speed(self, percent: float) -> None:
        self.requested_fan_speed = percent

    def set_last_command_result(self, success: bool, detail: str = "") -> None:
        self.last_command_result = HardwareCommandResult(
            success=success, detail=detail, timestamp=time.time(),
        )
