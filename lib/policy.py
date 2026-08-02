"""Converts system state into desired fan behavior.

Includes safety evaluation and emergency handling (see docs/build_order.md
Phase 5: "architecture.md's Business Logic layer names only Policy and
State"). Never touches hardware — it returns a decision for the Controller
to execute.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.models.config import FanCurveConfig, FanCurvePoint, SafetyConfig
from lib.state import OperatingMode, State

_OVER_TEMPERATURE_ALARM = "over_temperature"


@dataclass(frozen=True)
class FanDecision:
    fan_speed_percent: float
    mode: OperatingMode
    shutdown_requested: bool


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
    def __init__(self, fan_curve: FanCurveConfig, safety: SafetyConfig) -> None:
        self._fan_curve = fan_curve
        self._safety = safety

    def evaluate(self, state: State) -> FanDecision:
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

        if hottest >= self._safety.max_temperature:
            mode = OperatingMode.EMERGENCY
            target = 100.0
            shutdown_requested = self._safety.shutdown_on_emergency
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
