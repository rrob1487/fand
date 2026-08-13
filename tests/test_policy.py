"""Tests for lib/policy.py: the fan curve, the emergency band, and hysteresis.

Policy is where CLAUDE.md's safety requirements are actually decided. It is the
only thing that can ask for a shutdown, the only thing that pins the fans to
100%, and the only thing required to give the same answer for the same inputs.
It touches no hardware, so every test here is a plain function call.
"""

from __future__ import annotations

import unittest

from lib.models.config import FanCurveConfig, FanCurvePoint, SafetyConfig
from lib.policy import Policy, _interpolate_fan_percent
from lib.state import OperatingMode, State

_OVER_TEMPERATURE = "over_temperature"

# The curve shipped in config/config.toml.example, so these tests reason about
# the same numbers an operator actually deploys.
_EXAMPLE_POINTS = ((40, 20), (50, 30), (60, 40), (75, 70), (85, 100))


def _points(*pairs) -> tuple[FanCurvePoint, ...]:
    return tuple(FanCurvePoint(temperature_c=t, fan_percent=p) for t, p in pairs)


def _curve(*pairs, hysteresis=5.0) -> FanCurveConfig:
    """The example curve by default; pass (temp, percent) pairs to override."""
    return FanCurveConfig(
        points=_points(*(pairs or _EXAMPLE_POINTS)), hysteresis_percent=hysteresis,
    )


def _safety(max_temperature=90.0, shutdown=False, margin=0.0) -> SafetyConfig:
    return SafetyConfig(
        max_temperature=max_temperature,
        shutdown_on_emergency=shutdown,
        recovery_margin_c=margin,
    )


def _state(*readings, mode=None, requested=None) -> State:
    """A State carrying (name, value) readings, optionally mid-flight."""
    state = State()
    for name, value in readings:
        state.update_temperature(name, value)
    if mode is not None:
        state.set_mode(mode)
    if requested is not None:
        state.set_requested_fan_speed(requested)
    return state


class PolicyTestCase(unittest.TestCase):
    def policy(self, curve=None, safety=None) -> Policy:
        return Policy(
            curve if curve is not None else _curve(),
            safety if safety is not None else _safety(),
        )


# ---------------------------------------------------------------------------
# The curve
# ---------------------------------------------------------------------------
class InterpolationTests(unittest.TestCase):
    """An absent or exhausted curve must fail toward cooling, never toward
    silence."""

    def test_an_empty_curve_means_full_speed(self):
        # No curve is not "no cooling needed", it is "we do not know".
        self.assertEqual(_interpolate_fan_percent(50.0, ()), 100.0)

    def test_below_the_first_point_holds_the_first_percent(self):
        self.assertEqual(
            _interpolate_fan_percent(10.0, _points((40, 20), (85, 100))), 20.0,
        )

    def test_above_the_last_point_holds_the_last_percent(self):
        self.assertEqual(
            _interpolate_fan_percent(200.0, _points((40, 20), (85, 100))), 100.0,
        )

    def test_an_exact_point_returns_its_own_percent(self):
        points = _points(*_EXAMPLE_POINTS)
        for temperature, expected in _EXAMPLE_POINTS:
            with self.subTest(temperature=temperature):
                self.assertEqual(_interpolate_fan_percent(temperature, points), expected)

    def test_the_midpoint_of_a_segment_is_the_midpoint_of_its_percents(self):
        # 45C sits halfway between (40, 20) and (50, 30).
        self.assertEqual(
            _interpolate_fan_percent(45.0, _points((40, 20), (50, 30))), 25.0,
        )

    def test_interpolation_is_linear_across_a_segment(self):
        points = _points((40, 20), (60, 40))
        self.assertEqual(_interpolate_fan_percent(45.0, points), 25.0)
        self.assertEqual(_interpolate_fan_percent(55.0, points), 35.0)

    def test_points_given_out_of_order_are_sorted_first(self):
        scrambled = _points((85, 100), (40, 20), (60, 40))
        ordered = _points((40, 20), (60, 40), (85, 100))
        for temperature in (30.0, 45.0, 60.0, 70.0, 90.0):
            with self.subTest(temperature=temperature):
                self.assertEqual(
                    _interpolate_fan_percent(temperature, scrambled),
                    _interpolate_fan_percent(temperature, ordered),
                )

    def test_a_single_point_curve_is_flat(self):
        points = _points((60, 45))
        for temperature in (0.0, 60.0, 200.0):
            with self.subTest(temperature=temperature):
                self.assertEqual(_interpolate_fan_percent(temperature, points), 45.0)

    def test_duplicate_temperatures_never_divide_by_zero(self):
        # The zero-width-segment guard is defensive: the early-return branches
        # already cover every temperature that could reach it. So this sweeps
        # the range for a raise rather than asserting one value, which would
        # pin behaviour the code does not actually promise.
        points = _points((40, 20), (50, 30), (50, 60), (85, 100))
        for tenths in range(0, 1000):
            temperature = tenths / 10.0
            with self.subTest(temperature=temperature):
                result = _interpolate_fan_percent(temperature, points)
                self.assertGreaterEqual(result, 20.0)
                self.assertLessEqual(result, 100.0)

    def test_a_negative_reading_still_lands_on_the_curve(self):
        # A miswired or unplugged sensor can report below zero; the curve must
        # answer rather than raise.
        self.assertEqual(
            _interpolate_fan_percent(-40.0, _points(*_EXAMPLE_POINTS)), 20.0,
        )


# ---------------------------------------------------------------------------
# Mode selection
# ---------------------------------------------------------------------------
class ModeSelectionTests(PolicyTestCase):
    """RUNNING below the curve's top, WARNING in the band between the curve's
    top and the safety limit, EMERGENCY at or above it."""

    def test_below_the_curve_maximum_is_running(self):
        decision = self.policy().evaluate(_state(("CPU", 60.0)))
        self.assertIs(decision.mode, OperatingMode.RUNNING)
        self.assertEqual(decision.fan_speed_percent, 40.0)

    def test_at_the_curve_maximum_is_warning(self):
        decision = self.policy().evaluate(_state(("CPU", 85.0)))
        self.assertIs(decision.mode, OperatingMode.WARNING)
        self.assertEqual(decision.fan_speed_percent, 100.0)

    def test_inside_the_warning_band_is_warning(self):
        decision = self.policy().evaluate(_state(("CPU", 88.0)))
        self.assertIs(decision.mode, OperatingMode.WARNING)

    def test_at_the_safety_limit_is_emergency(self):
        decision = self.policy().evaluate(_state(("CPU", 90.0)))
        self.assertIs(decision.mode, OperatingMode.EMERGENCY)
        self.assertEqual(decision.fan_speed_percent, 100.0)

    def test_above_the_safety_limit_is_emergency(self):
        decision = self.policy().evaluate(_state(("CPU", 150.0)))
        self.assertIs(decision.mode, OperatingMode.EMERGENCY)
        self.assertEqual(decision.fan_speed_percent, 100.0)

    def test_the_hottest_sensor_decides(self):
        # Not the first, not the average, not the local one.
        decision = self.policy().evaluate(
            _state(("Inlet", 22.0), ("GPU", 91.0), ("Exhaust", 45.0)),
        )
        self.assertIs(decision.mode, OperatingMode.EMERGENCY)

    def test_a_gpu_alone_can_trigger_emergency(self):
        # The whole reason the daemon exists: iDRAC cannot see this sensor.
        decision = self.policy().evaluate(_state(("vm1 GPU", 95.0)))
        self.assertIs(decision.mode, OperatingMode.EMERGENCY)

    def test_without_a_curve_a_safe_reading_still_runs_full(self):
        policy = self.policy(curve=FanCurveConfig(points=()))
        decision = policy.evaluate(_state(("CPU", 20.0)))
        self.assertIs(decision.mode, OperatingMode.RUNNING)
        self.assertEqual(decision.fan_speed_percent, 100.0)


class UnknownTemperatureTests(PolicyTestCase):
    """No readings is the most dangerous input: the machine may be cooking and
    we cannot see it."""

    def test_no_readings_means_full_speed_and_emergency(self):
        decision = self.policy().evaluate(_state())
        self.assertEqual(decision.fan_speed_percent, 100.0)
        self.assertIs(decision.mode, OperatingMode.EMERGENCY)

    def test_no_readings_raises_the_over_temperature_alarm(self):
        state = _state()
        self.policy().evaluate(state)
        self.assertIn(_OVER_TEMPERATURE, state.alarms)

    def test_no_readings_does_not_request_a_shutdown(self):
        # Deliberate: an unknown temperature is not a known critical one, so
        # this branch cools hard but never powers the host off -- even with
        # shutdown_on_emergency enabled.
        policy = self.policy(safety=_safety(shutdown=True))
        self.assertFalse(policy.evaluate(_state()).shutdown_requested)

    def test_no_readings_ignores_hysteresis(self):
        # Damping a fallback to 100% would be damping the failsafe.
        state = _state(requested=20.0)
        self.assertEqual(self.policy().evaluate(state).fan_speed_percent, 100.0)

    def test_losing_every_sensor_mid_run_falls_back_to_full_speed(self):
        policy = self.policy()
        state = _state(("CPU", 45.0))
        self.assertIs(policy.evaluate(state).mode, OperatingMode.RUNNING)
        state.clear_temperature("CPU")
        self.assertEqual(policy.evaluate(state).fan_speed_percent, 100.0)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------
class ShutdownRequestTests(PolicyTestCase):
    """CLAUDE.md requirement 1. Policy only asks; the Controller acts."""

    def test_emergency_requests_shutdown_when_configured(self):
        policy = self.policy(safety=_safety(shutdown=True))
        self.assertTrue(policy.evaluate(_state(("CPU", 95.0))).shutdown_requested)

    def test_emergency_does_not_request_shutdown_by_default(self):
        self.assertFalse(
            self.policy().evaluate(_state(("CPU", 95.0))).shutdown_requested,
        )

    def test_a_latched_emergency_keeps_requesting_shutdown(self):
        policy = self.policy(safety=_safety(shutdown=True, margin=5.0))
        state = _state(("CPU", 95.0))
        policy.evaluate(state)
        state.update_temperature("CPU", 87.0)   # latched, below the limit
        decision = policy.evaluate(state)
        self.assertIs(decision.mode, OperatingMode.EMERGENCY)
        self.assertTrue(decision.shutdown_requested)

    def test_warning_never_requests_shutdown(self):
        policy = self.policy(safety=_safety(shutdown=True))
        decision = policy.evaluate(_state(("CPU", 88.0)))
        self.assertIs(decision.mode, OperatingMode.WARNING)
        self.assertFalse(decision.shutdown_requested)

    def test_running_never_requests_shutdown(self):
        policy = self.policy(safety=_safety(shutdown=True))
        decision = policy.evaluate(_state(("CPU", 50.0)))
        self.assertIs(decision.mode, OperatingMode.RUNNING)
        self.assertFalse(decision.shutdown_requested)


# ---------------------------------------------------------------------------
# The emergency latch
# ---------------------------------------------------------------------------
class EmergencyLatchTests(PolicyTestCase):
    """recovery_margin_c exists so a sensor sitting on the limit does not flap
    the mode and the fans every single cycle."""

    def setUp(self):
        self.policy_ = self.policy(safety=_safety(max_temperature=90.0, margin=5.0))

    def test_emergency_holds_above_the_recovery_point(self):
        state = _state(("CPU", 91.0))
        self.assertIs(self.policy_.evaluate(state).mode, OperatingMode.EMERGENCY)
        state.update_temperature("CPU", 87.0)   # below 90, above 85
        self.assertIs(self.policy_.evaluate(state).mode, OperatingMode.EMERGENCY)

    def test_emergency_holds_exactly_at_the_recovery_point(self):
        state = _state(("CPU", 91.0))
        self.policy_.evaluate(state)
        state.update_temperature("CPU", 85.0)   # >= 90 - 5, so still latched
        self.assertIs(self.policy_.evaluate(state).mode, OperatingMode.EMERGENCY)

    def test_emergency_releases_below_the_recovery_point(self):
        state = _state(("CPU", 91.0))
        self.policy_.evaluate(state)
        state.update_temperature("CPU", 84.9)
        self.assertIsNot(self.policy_.evaluate(state).mode, OperatingMode.EMERGENCY)

    def test_a_latched_emergency_stays_at_full_speed(self):
        state = _state(("CPU", 91.0))
        self.policy_.evaluate(state)
        state.update_temperature("CPU", 86.0)
        self.assertEqual(self.policy_.evaluate(state).fan_speed_percent, 100.0)

    def test_oscillating_on_the_limit_does_not_flap_the_mode(self):
        # The failure this margin exists to prevent: 89/91/89/91 would
        # otherwise toggle EMERGENCY and the fan target every cycle.
        state = _state(("CPU", 91.0))
        modes = []
        for temperature in (91.0, 89.0, 91.0, 89.0, 90.5, 88.0):
            state.update_temperature("CPU", temperature)
            modes.append(self.policy_.evaluate(state).mode)
        self.assertEqual(modes, [OperatingMode.EMERGENCY] * 6)

    def test_oscillating_on_the_limit_does_not_flap_the_fan_speed(self):
        state = _state(("CPU", 91.0))
        speeds = []
        for temperature in (91.0, 89.0, 91.0, 89.0):
            state.update_temperature("CPU", temperature)
            speeds.append(self.policy_.evaluate(state).fan_speed_percent)
        self.assertEqual(speeds, [100.0] * 4)

    def test_a_zero_margin_releases_as_soon_as_the_limit_is_cleared(self):
        policy = self.policy(safety=_safety(max_temperature=90.0, margin=0.0))
        state = _state(("CPU", 91.0))
        self.assertIs(policy.evaluate(state).mode, OperatingMode.EMERGENCY)
        state.update_temperature("CPU", 89.9)
        self.assertIsNot(policy.evaluate(state).mode, OperatingMode.EMERGENCY)

    def test_the_latch_does_not_engage_without_a_prior_emergency(self):
        # 87 is inside the recovery band but was never an emergency.
        self.assertIs(
            self.policy_.evaluate(_state(("CPU", 87.0))).mode, OperatingMode.WARNING,
        )

    def test_the_latch_lives_in_state_not_in_policy(self):
        # Two evaluations of separate States must not leak into each other --
        # this is what lets the daemon rebuild Policy on reload without
        # forgetting, or inventing, an emergency.
        hot = _state(("CPU", 95.0))
        self.policy_.evaluate(hot)
        fresh = _state(("CPU", 87.0))
        self.assertIs(self.policy_.evaluate(fresh).mode, OperatingMode.WARNING)


# ---------------------------------------------------------------------------
# Hysteresis
# ---------------------------------------------------------------------------
class HysteresisTests(PolicyTestCase):
    """Rising targets apply immediately; falling targets are damped. Cooling
    must never be delayed, only quieting."""

    def setUp(self):
        self.curve = _curve((40, 20), (85, 100), hysteresis=5.0)
        self.policy_ = self.policy(curve=self.curve)

    def _target_at(self, temperature: float) -> float:
        return _interpolate_fan_percent(temperature, self.curve.points)

    def test_the_first_decision_is_never_damped(self):
        state = _state(("CPU", 40.0))
        self.assertEqual(self.policy_.evaluate(state).fan_speed_percent, 20.0)

    def test_a_rise_applies_immediately(self):
        state = _state(("CPU", 76.0), requested=20.0)
        self.assertGreater(self.policy_.evaluate(state).fan_speed_percent, 20.0)

    def test_a_small_drop_holds_the_previous_speed(self):
        state = _state(("CPU", 70.0), requested=self._target_at(70.0) + 2.0)
        self.assertEqual(
            self.policy_.evaluate(state).fan_speed_percent, self._target_at(70.0) + 2.0,
        )

    def test_a_large_drop_applies(self):
        state = _state(("CPU", 45.0), requested=80.0)
        self.assertEqual(
            self.policy_.evaluate(state).fan_speed_percent, self._target_at(45.0),
        )

    def test_a_drop_of_exactly_the_threshold_applies(self):
        target = self._target_at(60.0)
        state = _state(("CPU", 60.0), requested=target + 5.0)
        self.assertEqual(self.policy_.evaluate(state).fan_speed_percent, target)

    def test_a_drop_just_under_the_threshold_is_damped(self):
        target = self._target_at(60.0)
        previous = target + 4.999
        state = _state(("CPU", 60.0), requested=previous)
        self.assertEqual(self.policy_.evaluate(state).fan_speed_percent, previous)

    def test_an_equal_target_is_left_alone(self):
        target = self._target_at(60.0)
        state = _state(("CPU", 60.0), requested=target)
        self.assertEqual(self.policy_.evaluate(state).fan_speed_percent, target)

    def test_repeated_small_drops_do_not_creep_downward(self):
        # Each cycle re-compares against the held value, not against the last
        # computed target, so a steady temperature stays put forever.
        held = self._target_at(70.0) + 2.0
        state = _state(("CPU", 70.0), requested=held)
        for _ in range(10):
            speed = self.policy_.evaluate(state).fan_speed_percent
        self.assertEqual(speed, held)

    def test_emergency_bypasses_damping_entirely(self):
        # Contrast the two modes on the same numbers: a 2-point drop is held in
        # RUNNING and passed straight through in EMERGENCY.
        self.assertEqual(
            self.policy_._apply_hysteresis(98.0, 100.0, OperatingMode.RUNNING), 100.0,
        )
        self.assertEqual(
            self.policy_._apply_hysteresis(98.0, 100.0, OperatingMode.EMERGENCY), 98.0,
        )

    def test_a_zero_threshold_damps_nothing(self):
        curve = _curve((40, 20), (85, 100), hysteresis=0.0)
        policy = self.policy(curve=curve)
        target = _interpolate_fan_percent(60.0, curve.points)
        state = _state(("CPU", 60.0), requested=target + 0.001)
        self.assertEqual(policy.evaluate(state).fan_speed_percent, target)


# ---------------------------------------------------------------------------
# State side effects
# ---------------------------------------------------------------------------
class StateSideEffectTests(PolicyTestCase):
    """evaluate() writes its conclusion back into State. The Controller and the
    notification subsystem both read it from there."""

    def test_the_mode_is_written_to_state(self):
        state = _state(("CPU", 95.0))
        decision = self.policy().evaluate(state)
        self.assertIs(state.mode, decision.mode)

    def test_the_final_damped_speed_is_written_to_state(self):
        # Not the pre-hysteresis target: State must agree with the decision.
        curve = _curve((40, 20), (85, 100), hysteresis=5.0)
        policy = self.policy(curve=curve)
        # 70C targets 73.3%; from 75% that is a 1.7-point drop, so it is held.
        state = _state(("CPU", 70.0), requested=75.0)
        decision = policy.evaluate(state)
        self.assertEqual(state.requested_fan_speed, decision.fan_speed_percent)
        self.assertEqual(state.requested_fan_speed, 75.0)
        self.assertNotEqual(
            state.requested_fan_speed, _interpolate_fan_percent(70.0, curve.points),
        )

    def test_emergency_raises_the_over_temperature_alarm(self):
        state = _state(("CPU", 95.0))
        self.policy().evaluate(state)
        self.assertIn(_OVER_TEMPERATURE, state.alarms)

    def test_leaving_emergency_clears_the_alarm(self):
        state = _state(("CPU", 95.0))
        policy = self.policy()
        policy.evaluate(state)
        state.update_temperature("CPU", 50.0)
        policy.evaluate(state)
        self.assertNotIn(_OVER_TEMPERATURE, state.alarms)

    def test_warning_does_not_raise_the_alarm(self):
        state = _state(("CPU", 88.0))
        self.policy().evaluate(state)
        self.assertNotIn(_OVER_TEMPERATURE, state.alarms)

    def test_unrelated_alarms_are_left_alone(self):
        # Sensor-failure alarms belong to SensorManager; Policy must not eat them.
        state = _state(("CPU", 50.0))
        state.set_alarm("sensor_failure:vm1 GPU")
        self.policy().evaluate(state)
        self.assertIn("sensor_failure:vm1 GPU", state.alarms)

    def test_evaluate_does_not_invent_readings(self):
        state = _state(("CPU", 50.0))
        self.policy().evaluate(state)
        self.assertEqual(set(state.temperatures), {"CPU"})


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
class DeterminismTests(PolicyTestCase):
    """CLAUDE.md design principle 6: same inputs, same decision, every time.
    No hidden state, no learning."""

    def test_two_policies_agree_on_identical_input(self):
        for temperature in (20.0, 45.0, 85.0, 89.9, 90.0, 200.0):
            with self.subTest(temperature=temperature):
                first = self.policy().evaluate(_state(("CPU", temperature)))
                second = self.policy().evaluate(_state(("CPU", temperature)))
                self.assertEqual(first, second)

    def test_a_steady_temperature_yields_a_steady_decision(self):
        policy = self.policy()
        state = _state(("CPU", 62.0))
        decisions = [policy.evaluate(state) for _ in range(5)]
        self.assertEqual(len(set(decisions)), 1)

    def test_sensor_ordering_does_not_change_the_decision(self):
        readings = [("A", 30.0), ("B", 88.0), ("C", 55.0)]
        forward = self.policy().evaluate(_state(*readings))
        backward = self.policy().evaluate(_state(*reversed(readings)))
        self.assertEqual(forward, backward)

    def test_the_decision_is_immutable(self):
        decision = self.policy().evaluate(_state(("CPU", 50.0)))
        with self.assertRaises(Exception):
            decision.fan_speed_percent = 0.0


if __name__ == "__main__":
    unittest.main()
