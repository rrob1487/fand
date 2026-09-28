"""Converts system state into desired fan behavior.

Includes safety evaluation and emergency handling (see docs/build_order.md
Phase 5: "architecture.md's Business Logic layer names only Policy and
State"). Never touches hardware — it returns a decision for the Controller
to execute.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from lib.models.config import FanCurveConfig, FanCurvePoint, SafetyConfig
from lib.state import OperatingMode, State

_OVER_TEMPERATURE_ALARM = "over_temperature"


@dataclass(frozen=True)
class FanDecision:
    # None when automatic_control is set: iDRAC picks the speed, not us.
    fan_speed_percent: float | None
    mode: OperatingMode
    shutdown_requested: bool
    automatic_control: bool = False


def _interpolate_fan_percent(temperature: float, points: tuple[FanCurvePoint, ...]) -> float:
    if not points:
        return 100.0  # no curve defined: fail safe to max cooling

    sorted_points = sorted(points, key=lambda p: p.temperature_c)
    if temperature <= sorted_points[0].temperature_c:
        return sorted_points[0].fan_percent
    if temperature >= sorted_points[-1].temperature_c:
        return sorted_points[-1].fan_percent

    for lower, upper in zip(sorted_points, sorted_points[1:]):
        if lower.temperature_c <= temperature <= upper.temperature_c:
            span = upper.temperature_c - lower.temperature_c
            if span == 0:
                return upper.fan_percent
            ratio = (temperature - lower.temperature_c) / span
            return lower.fan_percent + ratio * (upper.fan_percent - lower.fan_percent)

    return sorted_points[-1].fan_percent  # unreachable safeguard


class Policy:
    def __init__(
        self,
        fan_curve: FanCurveConfig,
        safety: SafetyConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fan_curve = fan_curve
        self._safety = safety
        # Must be the clock State.mark_sensor_lost() stamps with. Injectable
        # so the grace period is testable without sleeping.
        self._clock = clock

    def _overdue_lost_sensors(self, state: State) -> tuple[str, ...]:
        now = self._clock()
        grace = self._safety.sensor_lost_grace_seconds
        return tuple(sorted(
            name for name, since in state.lost_sensors.items() if now - since >= grace
        ))

    def evaluate(self, state: State) -> FanDecision:
        # Recorded whatever the mode, so a notification sent during an
        # EMERGENCY still says which sensors the daemon has lost sight of.
        unmonitored = self._overdue_lost_sensors(state)
        state.set_unmonitored_sensors(unmonitored)

        if not state.temperatures:
            # Unknown temperature: fail safe to max cooling, no hysteresis.
            decision = FanDecision(
                fan_speed_percent=100.0, mode=OperatingMode.EMERGENCY, shutdown_requested=False,
            )
            state.set_alarm(_OVER_TEMPERATURE_ALARM)
            state.set_mode(decision.mode)
            state.set_requested_fan_speed(decision.fan_speed_percent)
            return decision

        hottest = max(reading.value for reading in state.temperatures.values())
        curve_max_temp = (
            max(point.temperature_c for point in self._fan_curve.points)
            if self._fan_curve.points
            else None
        )

        was_emergency = state.mode is OperatingMode.EMERGENCY
        emergency_exit_temp = self._safety.max_temperature - self._safety.recovery_margin_c

        if hottest >= self._safety.max_temperature:
            mode = OperatingMode.EMERGENCY
            target = 100.0
            shutdown_requested = self._safety.shutdown_on_emergency
        elif was_emergency and hottest >= emergency_exit_temp:
            # Stay in EMERGENCY until temperature drops below the recovery
            # margin, not just below max_temperature, so a sensor hovering
            # at the limit doesn't flap mode/fan speed every cycle.
            mode = OperatingMode.EMERGENCY
            target = 100.0
            shutdown_requested = self._safety.shutdown_on_emergency
        elif unmonitored:
            # Checked only after both EMERGENCY branches: a sensor we can still
            # see at the limit gets 100% and a shutdown, never a handover.
            # Otherwise the curve would be driven by an incomplete picture --
            # dropping the hottest sensor lowers the fans exactly when they
            # may be needed -- so iDRAC gets the fans back until it returns.
            state.clear_alarm(_OVER_TEMPERATURE_ALARM)
            state.set_mode(OperatingMode.FAILSAFE)
            state.set_requested_fan_speed(None)
            return FanDecision(
                fan_speed_percent=None,
                mode=OperatingMode.FAILSAFE,
                shutdown_requested=False,
                automatic_control=True,
            )
        elif curve_max_temp is not None and hottest >= curve_max_temp:
            mode = OperatingMode.WARNING
            target = _interpolate_fan_percent(hottest, self._fan_curve.points)
            shutdown_requested = False
        else:
            mode = OperatingMode.RUNNING
            target = _interpolate_fan_percent(hottest, self._fan_curve.points)
            shutdown_requested = False

        final_target = self._apply_hysteresis(target, state.requested_fan_speed, mode)

        if mode is OperatingMode.EMERGENCY:
            state.set_alarm(_OVER_TEMPERATURE_ALARM)
        else:
            state.clear_alarm(_OVER_TEMPERATURE_ALARM)
        state.set_mode(mode)
        state.set_requested_fan_speed(final_target)

        return FanDecision(
            fan_speed_percent=final_target, mode=mode, shutdown_requested=shutdown_requested,
        )

    def _apply_hysteresis(
        self, target: float, previous: float | None, mode: OperatingMode,
    ) -> float:
        if mode is OperatingMode.EMERGENCY or previous is None:
            return target
        if target >= previous:
            return target  # always allow raising immediately
        if previous - target >= self._fan_curve.hysteresis_percent:
            return target
        return previous  # damp small decreases
