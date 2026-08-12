"""Trigger criteria: whether a notifier is active right now.

Pure evaluation — no clock, no scheduling, no queueing. A trigger answers "is
the condition met for this snapshot?" and nothing else; the notifier turns that
answer into a schedule.

Each trigger applies its own sensor selection when evaluating, rather than
relying on the caller to have filtered first. That means the notifier filters
again when it builds the payload, which is deliberate: an implicit
"pass me a pre-scoped notification" precondition would fail silently rather
than loudly, firing a notifier on a sensor it was configured to ignore.
Filtering costs a comprehension over a handful of readings, and
`with_sensors(None)` returns the snapshot unchanged, so the unscoped case pays
nothing at all.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lib.notifications.notification import Notification


class Trigger(ABC):
    """The conditions under which one notifier generates notification jobs."""

    def __init__(self, sensors: tuple[str, ...] | None) -> None:
        self._sensors = sensors

    @property
    def sensor_names(self) -> tuple[str, ...] | None:
        """Sensors this trigger considers, or None for every available sensor.

        Scopes both this trigger's evaluation and the payload the notifier
        builds from it.
        """
        return self._sensors

    @abstractmethod
    def is_active(self, notification: Notification) -> bool:
        """Whether the criteria are met for this snapshot."""


class ThresholdTrigger(Trigger):
    """Active while the hottest selected sensor is at or above a temperature."""

    def __init__(self, sensors: tuple[str, ...] | None, temperature_c: float) -> None:
        super().__init__(sensors)
        self._temperature_c = temperature_c

    def is_active(self, notification: Notification) -> bool:
        hottest = notification.with_sensors(self._sensors).hottest
        if hottest is None:
            # No reading available for any selected sensor. A notifier that
            # cannot see a temperature cannot claim a threshold was crossed.
            return False
        return hottest.value_c >= self._temperature_c


class GeneralTrigger(Trigger):
    """Always active; the notifier fires purely on its interval.

    Returning True unconditionally is what lets the notifier run one scheduling
    path for every trigger type: a general notifier is simply one whose rising
    edge lands on the first cycle and never falls.
    """

    def is_active(self, notification: Notification) -> bool:
        return True
