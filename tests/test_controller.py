"""Tests for lib/controller.py, focused on notification integration.

The fakes below are the smallest things that satisfy the collaborators'
interfaces. They share one call log so ordering within a cycle can be asserted
rather than inferred from reading the source.
"""

from __future__ import annotations

import unittest

from lib.controller import Controller
from lib.policy import FanDecision
from lib.state import OperatingMode, State


class FakeSensorManager:
    def __init__(self, log, temperatures=(("CPU1 Temp", 80.0),)):
        self._log = log
        self._temperatures = temperatures

    def poll(self, state: State) -> None:
        self._log.append("poll")
        for name, value in self._temperatures:
            state.update_temperature(name, value)


class FakePolicy:
    def __init__(self, log, decision):
        self._log = log
        self._decision = decision

    def evaluate(self, state: State) -> FanDecision:
        self._log.append("evaluate")
        state.set_mode(self._decision.mode)
        state.set_requested_fan_speed(self._decision.fan_speed_percent)
        return self._decision


class FakeFanController:
    def __init__(self, log):
        self._log = log
        self.speeds = []

    def set_speed(self, percent: float) -> None:
        self._log.append("set_speed")
        self.speeds.append(percent)

    def enable_automatic_control(self) -> None:
        self._log.append("enable_automatic_control")


class FakeNotificationManager:
    def __init__(self, log, error=None):
        self._log = log
        self._error = error
        self.states = []

    def dispatch(self, state: State) -> None:
        self._log.append("dispatch")
        self.states.append(state)
        if self._error is not None:
            raise self._error


def _decision(speed=70.0, mode=OperatingMode.RUNNING, shutdown=False) -> FanDecision:
    return FanDecision(
        fan_speed_percent=speed, mode=mode, shutdown_requested=shutdown,
    )


class ControllerTestCase(unittest.TestCase):
    def build(self, decision=None, manager=None, **kwargs) -> Controller:
        self.log: list[str] = []
        self.sensors = FakeSensorManager(self.log)
        self.fans = FakeFanController(self.log)
        self.manager = manager
        controller = Controller(
            self.sensors,
            FakePolicy(self.log, decision or _decision()),
            self.fans,
            notification_manager=manager,
            **kwargs,
        )
        controller._shutdown_host = lambda: self.log.append("shutdown")
        return controller


class WiringTests(ControllerTestCase):
    def test_dispatch_is_called_once_per_cycle(self):
        manager = FakeNotificationManager([])
        controller = self.build(manager=manager)
        manager._log = self.log
        controller.run_cycle()
        controller.run_cycle()
        self.assertEqual(self.log.count("dispatch"), 2)

    def test_dispatch_receives_the_controllers_own_state(self):
        manager = FakeNotificationManager([])
        controller = self.build(manager=manager)
        manager._log = self.log
        controller.run_cycle()
        self.assertIs(manager.states[0], controller.state)

    def test_state_is_fully_updated_before_dispatch(self):
        manager = FakeNotificationManager([])
        controller = self.build(_decision(speed=85.0), manager=manager)
        manager._log = self.log
        controller.run_cycle()
        state = manager.states[0]
        self.assertEqual(state.requested_fan_speed, 85.0)
        self.assertIn("CPU1 Temp", state.temperatures)
        self.assertIsNotNone(state.last_command_result)


class OrderingTests(ControllerTestCase):
    def test_dispatch_comes_after_the_fan_speed_is_applied(self):
        manager = FakeNotificationManager([])
        controller = self.build(manager=manager)
        manager._log = self.log
        controller.run_cycle()
        self.assertLess(self.log.index("set_speed"), self.log.index("dispatch"))

    def test_dispatch_comes_after_the_shutdown_request(self):
        """Nothing may sit between an emergency decision and the shutdown that
        answers it."""
        manager = FakeNotificationManager([])
        controller = self.build(
            _decision(speed=100.0, mode=OperatingMode.EMERGENCY, shutdown=True),
            manager=manager,
        )
        manager._log = self.log
        controller.run_cycle()
        self.assertEqual(
            self.log, ["poll", "evaluate", "set_speed", "shutdown", "dispatch"],
        )

    def test_full_cycle_order(self):
        manager = FakeNotificationManager([])
        controller = self.build(manager=manager)
        manager._log = self.log
        controller.run_cycle()
        self.assertEqual(self.log, ["poll", "evaluate", "set_speed", "dispatch"])


class IsolationTests(ControllerTestCase):
    """Completion criteria: a broken manager, and no manager at all, both
    leave the control loop's behaviour unchanged."""

    def test_a_raising_manager_does_not_break_the_cycle(self):
        manager = FakeNotificationManager([], error=RuntimeError("boom"))
        controller = self.build(_decision(speed=85.0), manager=manager)
        manager._log = self.log
        with self.assertLogs("lib.controller", level="WARNING") as logs:
            controller.run_cycle()
        self.assertEqual(self.fans.speeds, [85.0])
        self.assertEqual(self.log, ["poll", "evaluate", "set_speed", "dispatch"])
        self.assertTrue([x for x in logs.output if "dispatch failed" in x])

    def test_a_raising_manager_does_not_prevent_shutdown(self):
        manager = FakeNotificationManager([], error=RuntimeError("boom"))
        controller = self.build(
            _decision(speed=100.0, mode=OperatingMode.EMERGENCY, shutdown=True),
            manager=manager,
        )
        manager._log = self.log
        with self.assertLogs("lib.controller", level="WARNING"):
            controller.run_cycle()
        self.assertIn("shutdown", self.log)

    def test_a_raising_manager_does_not_stop_later_cycles(self):
        manager = FakeNotificationManager([], error=RuntimeError("boom"))
        controller = self.build(manager=manager)
        manager._log = self.log
        with self.assertLogs("lib.controller", level="WARNING"):
            controller.run_cycle()
            controller.run_cycle()
        self.assertEqual(self.fans.speeds, [70.0, 70.0])

    def test_without_a_manager_the_cycle_is_unchanged(self):
        controller = self.build(_decision(speed=85.0))
        controller.run_cycle()
        self.assertEqual(self.log, ["poll", "evaluate", "set_speed"])
        self.assertEqual(self.fans.speeds, [85.0])

    def test_without_a_manager_shutdown_still_happens(self):
        controller = self.build(
            _decision(speed=100.0, mode=OperatingMode.EMERGENCY, shutdown=True)
        )
        controller.run_cycle()
        self.assertEqual(self.log, ["poll", "evaluate", "set_speed", "shutdown"])

    def test_manager_defaults_to_none(self):
        # Controller stays constructible without the subsystem.
        controller = Controller(
            FakeSensorManager([]), FakePolicy([], _decision()), FakeFanController([]),
        )
        self.assertIsNone(controller._notification_manager)


class DryRunTests(ControllerTestCase):
    def test_dispatch_still_happens_in_dry_run(self):
        # Suppressing delivery is the notifier's job; the controller always
        # hands over state so a dry run shows what would have been sent.
        manager = FakeNotificationManager([])
        controller = self.build(manager=manager, dry_run=True)
        manager._log = self.log
        controller.run_cycle()
        self.assertIn("dispatch", self.log)

    def test_dry_run_does_not_touch_the_fan_controller(self):
        controller = self.build(dry_run=True)
        controller.run_cycle()
        self.assertEqual(self.fans.speeds, [])


if __name__ == "__main__":
    unittest.main()
