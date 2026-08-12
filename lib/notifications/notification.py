"""The generic notification representation.

A fully detached snapshot of primitives. `State` is mutable and is rewritten by
the controller thread every cycle, so handing a live view of it to a worker
thread would be a data race whose symptom is a notification reporting a
temperature that never existed. Building an immutable copy at dispatch time
makes that impossible rather than unlikely.

Endpoint implementations translate this into their own external format; nothing
here knows what any of them look like.
"""

from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class SensorReading:
    name: str
    value_c: float
    timestamp: float


@dataclass(frozen=True)
class Notification:
    timestamp: float
    readings: tuple[SensorReading, ...]
    fan_speed_percent: float | None
    operating_mode: str
    alarms: tuple[str, ...]
    last_command_ok: bool | None

    @property
    def sensor_names(self) -> tuple[str, ...]:
        return tuple(reading.name for reading in self.readings)

    @property
    def hottest(self) -> SensorReading | None:
        """The highest reading, or None when no sensor data is available.

        A derived view of immutable data, not a decision: the threshold trigger
        compares it against its configured temperature and the Discord endpoint
        puts it in the headline, and neither should re-derive it.
        """
        if not self.readings:
            return None
        return max(self.readings, key=lambda reading: reading.value_c)

    def with_sensors(self, names: tuple[str, ...] | None) -> "Notification":
        """A copy carrying only the named sensors, in the order given.

        `None` selects everything and returns this notification unchanged.
        Names with no reading available are dropped silently — the caller knows
        which sensors it asked for and reports the gap, so this stays pure data.
        """
        if names is None:
            return self
        by_name = {reading.name: reading for reading in self.readings}
        selected = tuple(by_name[name] for name in names if name in by_name)
        return replace(self, readings=selected)
