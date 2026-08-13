"""Tests for lib/controller.py: the control cycle and the actions it takes.

The fakes below are the smallest things that satisfy the collaborators'
interfaces. They share one call log so ordering within a cycle can be asserted
rather than inferred from reading the source.

Nothing here reaches hardware. The fan controller is a fake, and the one place
that shells out -- the emergency poweroff -- has subprocess.run replaced by a
recorder, so the tests assert the exact command without ever running it.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from lib.controller import Controller
from lib.hardware.ipmi import IPMIError
from lib.policy import FanDecision
from lib.state import OperatingMode, State

_LOGGER = "lib.controller"
_POWEROFF = ["sudo", "-n", "systemctl", "poweroff", "--ignore-inhibitors"]


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
    def __init__(self, log, set_errors=(), release_error=None):
        self._log = log
        self.speeds = []
        self.releases = 0
        # One entry consumed per call, so "fails twice then succeeds" and
        # "always fails" are both expressible against the retry decorator.
        self._set_errors = list(set_errors)
        self._release_error = release_error

    def set_speed(self, percent: float) -> None:
        self._log.append("set_speed")
        self.speeds.append(percent)
        if self._set_errors:
            raise self._set_errors.pop(0)

    def enable_automatic_control(self) -> None:
        self._log.append("enable_automatic_control")
        self.releases += 1
        if self._release_error is not None:
            raise self._release_error


class RecordingRun:
    """Stands in for subprocess.run so the poweroff command is asserted, never
    executed."""

    def __init__(self, log=None, error=None):
        self.calls: list[tuple[list[str], dict]] = []
        self._log = log
        self._error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._log is not None:
            self._log.append("poweroff")
        if self._error is not None:
            raise self._error
        return subprocess.CompletedProcess(argv, 0)

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]


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
    def build(
        self, decision=None, manager=None, fans=None, stub_shutdown=True, **kwargs,
    ) -> Controller:
        self.log: list[str] = []
        self.sensors = FakeSensorManager(self.log)
        self.fans = fans if fans is not None else FakeFanController(self.log)
        self.fans._log = self.log
        self.manager = manager
        if manager is not None:
            manager._log = self.log   # share the one ordering log
        controller = Controller(
            self.sensors,
            FakePolicy(self.log, decision or _decision()),
            self.fans,
            notification_manager=manager,
            **kwargs,
        )
        if stub_shutdown:
            # Most tests only care that a shutdown was asked for. The tests
            # that exercise the real method pass stub_shutdown=False.
            controller._shutdown_host = lambda: self.log.append("shutdown")
        return controller

    def no_backoff(self) -> None:
        """Skip the retry decorator's real 0.5s/1.0s waits.

        The ramp governs how long a flaky BMC gets to answer, not whether the
        retry works, so no assertion here depends on its value.
        """
        patcher = patch("lib.utils.retry.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def patch_run(self, **kwargs) -> RecordingRun:
        fake = RecordingRun(**kwargs)
        patcher = patch("lib.controller.subprocess.run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


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


class FanSpeedTests(ControllerTestCase):
    """Applying the decision. A BMC that stops answering must be recorded and
    survived, never allowed to end the cycle."""

    def setUp(self):
        self.no_backoff()

    def test_the_decided_speed_is_applied(self):
        controller = self.build(_decision(speed=64.0))
        controller.run_cycle()
        self.assertEqual(self.fans.speeds, [64.0])

    def test_a_success_is_recorded(self):
        controller = self.build(_decision(speed=64.0))
        controller.run_cycle()
        result = controller.state.last_command_result
        self.assertTrue(result.success)
        self.assertIn("64", result.detail)

    def test_a_failure_is_recorded_rather_than_raised(self):
        fans = FakeFanController([], set_errors=[IPMIError("BMC busy")] * 3)
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertFalse(controller.state.last_command_result.success)

    def test_a_failure_records_the_reason(self):
        fans = FakeFanController([], set_errors=[IPMIError("BMC busy")] * 3)
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertIn("BMC busy", controller.state.last_command_result.detail)

    def test_a_failure_is_logged_at_error(self):
        fans = FakeFanController([], set_errors=[IPMIError("BMC busy")] * 3)
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR") as logs:
            controller.run_cycle()
        self.assertTrue([line for line in logs.output if "failed to set fan speed" in line])

    def test_a_failed_cycle_does_not_stop_the_next_one(self):
        # One unanswered command must not end fan control for good.
        fans = FakeFanController([], set_errors=[IPMIError("BMC busy")] * 3)
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        controller.run_cycle()
        self.assertTrue(controller.state.last_command_result.success)

    def test_dry_run_records_what_it_would_have_done(self):
        controller = self.build(_decision(speed=64.0), dry_run=True)
        controller.run_cycle()
        result = controller.state.last_command_result
        self.assertTrue(result.success)
        self.assertIn("dry-run", result.detail)
        self.assertIn("64", result.detail)


class FanSpeedRetryTests(ControllerTestCase):
    """A transient BMC failure should not be reported as a lost fan command."""

    def setUp(self):
        self.no_backoff()

    def test_a_command_that_recovers_within_its_retries_succeeds(self):
        fans = FakeFanController([], set_errors=[IPMIError("busy"), IPMIError("busy")])
        controller = self.build(fans=fans)
        controller.run_cycle()
        self.assertTrue(controller.state.last_command_result.success)

    def test_a_command_is_attempted_three_times_before_giving_up(self):
        fans = FakeFanController([], set_errors=[IPMIError("busy")] * 3)
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertEqual(len(fans.speeds), 3)

    def test_backoff_waits_between_attempts_but_not_after_the_last(self):
        fans = FakeFanController([], set_errors=[IPMIError("busy")] * 3)
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [0.5, 1.0])

    def test_a_giving_up_command_never_escapes_the_cycle(self):
        # It has to reach _apply_fan_speed's handler, not the caller.
        fans = FakeFanController([], set_errors=[IPMIError("busy")] * 3)
        controller = self.build(fans=fans, manager=FakeNotificationManager([]))
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertIn("dispatch", self.log)


class ReleaseFanControlTests(ControllerTestCase):
    """CLAUDE.md non-negotiable 3: daemon exit always releases fan control back
    to iDRAC. This method is called from teardown, where nothing may raise."""

    def test_control_is_handed_back_to_automatic_mode(self):
        controller = self.build()
        controller.release_fan_control()
        self.assertEqual(self.log, ["enable_automatic_control"])

    def test_the_release_is_logged(self):
        controller = self.build()
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.release_fan_control()
        self.assertTrue([line for line in logs.output if "Released fan control" in line])

    def test_an_ipmi_failure_is_swallowed(self):
        # Raising here would abort teardown and could leave the notification
        # subsystem running, or the process wedged, on the way out.
        fans = FakeFanController([], release_error=IPMIError("BMC unreachable"))
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.release_fan_control()

    def test_a_failed_release_is_logged_at_error(self):
        fans = FakeFanController([], release_error=IPMIError("BMC unreachable"))
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR") as logs:
            controller.release_fan_control()
        self.assertTrue(
            [line for line in logs.output if "restore automatic fan control" in line],
        )

    def test_it_can_be_called_more_than_once(self):
        # teardown may run from both the run() finally block and a cleanup.
        controller = self.build()
        controller.release_fan_control()
        controller.release_fan_control()
        self.assertEqual(self.fans.releases, 2)

    def test_dry_run_touches_no_hardware(self):
        controller = self.build(dry_run=True)
        with self.assertLogs(_LOGGER, level="INFO"):
            controller.release_fan_control()
        self.assertEqual(self.fans.releases, 0)

    def test_releasing_is_not_retried(self):
        # Deliberate contrast with set_speed: teardown must be prompt, and
        # fand.service's ExecStopPost is the real backstop if this fails.
        fans = FakeFanController([], release_error=IPMIError("BMC unreachable"))
        controller = self.build(fans=fans)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.release_fan_control()
        self.assertEqual(fans.releases, 1)


class ShutdownHostTests(ControllerTestCase):
    """The real _shutdown_host, which every other test in this file replaces.

    A test can prove which command is issued; only the R730 can prove the host
    actually powers off. Note that fand.service runs with NoNewPrivileges=true
    and an empty CapabilityBoundingSet, which blocks setuid binaries -- so sudo
    here is worth verifying on the target before shutdown_on_emergency is
    turned on.
    """

    def emergency(self, **kwargs) -> Controller:
        return self.build(
            _decision(speed=100.0, mode=OperatingMode.EMERGENCY, shutdown=True),
            stub_shutdown=False,
            **kwargs,
        )

    def test_the_poweroff_command_is_issued(self):
        run = self.patch_run()
        controller = self.emergency()
        with self.assertLogs(_LOGGER, level="CRITICAL"):
            controller.run_cycle()
        self.assertEqual(run.argv, _POWEROFF)

    def test_the_command_is_not_run_without_a_shutdown_request(self):
        run = self.patch_run()
        self.build(stub_shutdown=False).run_cycle()
        self.assertEqual(run.calls, [])

    def test_sudo_is_non_interactive(self):
        # -n: fail immediately rather than block forever on a password prompt
        # that will never come, on a host that is overheating.
        run = self.patch_run()
        with self.assertLogs(_LOGGER, level="CRITICAL"):
            self.emergency().run_cycle()
        self.assertIn("-n", run.argv)

    def test_inhibitors_are_ignored(self):
        # A forgotten SSH session must not veto a thermal shutdown.
        run = self.patch_run()
        with self.assertLogs(_LOGGER, level="CRITICAL"):
            self.emergency().run_cycle()
        self.assertIn("--ignore-inhibitors", run.argv)

    def test_a_failure_is_surfaced_by_the_exit_code(self):
        run = self.patch_run()
        with self.assertLogs(_LOGGER, level="CRITICAL"):
            self.emergency().run_cycle()
        self.assertTrue(run.calls[-1][1]["check"])

    def test_the_shutdown_is_logged_at_critical(self):
        self.patch_run()
        controller = self.emergency()
        with self.assertLogs(_LOGGER, level="CRITICAL") as logs:
            controller.run_cycle()
        self.assertTrue([line for line in logs.output if "EMERGENCY" in line])

    def test_a_refused_poweroff_does_not_raise(self):
        # sudo failing (as it would under NoNewPrivileges) must not crash the
        # daemon; the fans are already at 100% and must stay driven.
        self.patch_run(error=subprocess.CalledProcessError(1, _POWEROFF))
        controller = self.emergency()
        with self.assertLogs(_LOGGER, level="ERROR") as logs:
            controller.run_cycle()
        self.assertTrue([line for line in logs.output if "failed to issue host shutdown" in line])

    def test_a_missing_systemctl_does_not_raise(self):
        self.patch_run(error=FileNotFoundError("no sudo"))
        controller = self.emergency()
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()

    def test_a_failed_shutdown_still_finishes_the_cycle(self):
        self.patch_run(error=subprocess.CalledProcessError(1, _POWEROFF))
        manager = FakeNotificationManager([])
        controller = self.emergency(manager=manager)
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertIn("dispatch", self.log)

    def test_dry_run_issues_nothing(self):
        run = self.patch_run()
        controller = self.emergency(dry_run=True)
        with self.assertLogs(_LOGGER, level="INFO"):
            controller.run_cycle()
        self.assertEqual(run.calls, [])


class CoolingWinsTheRaceTests(ControllerTestCase):
    """CLAUDE.md non-negotiable 2: a critical condition forces the fans to
    maximum immediately, before or alongside the shutdown."""

    def setUp(self):
        self.no_backoff()

    def emergency(self, **kwargs) -> Controller:
        return self.build(
            _decision(speed=100.0, mode=OperatingMode.EMERGENCY, shutdown=True),
            stub_shutdown=False,
            **kwargs,
        )

    def test_the_fans_are_commanded_before_the_poweroff(self):
        controller = self.emergency()
        run = self.patch_run(log=self.log)   # self.log exists once build() ran
        with self.assertLogs(_LOGGER, level="CRITICAL"):
            controller.run_cycle()
        self.assertLess(self.log.index("set_speed"), self.log.index("poweroff"))
        self.assertEqual(run.argv, _POWEROFF)

    def test_the_fans_are_commanded_to_full(self):
        self.patch_run()
        controller = self.emergency()
        with self.assertLogs(_LOGGER, level="CRITICAL"):
            controller.run_cycle()
        self.assertEqual(self.fans.speeds, [100.0])

    def test_a_failing_fan_command_does_not_cancel_the_shutdown(self):
        # Both halves of the safety requirement are independent: if cooling
        # cannot be applied, powering off matters more, not less.
        fans = FakeFanController([], set_errors=[IPMIError("BMC busy")] * 3)
        controller = self.emergency(fans=fans)
        run = self.patch_run()
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertEqual(run.argv, _POWEROFF)

    def test_the_fans_are_tried_three_times_before_the_shutdown_proceeds(self):
        fans = FakeFanController([], set_errors=[IPMIError("BMC busy")] * 3)
        controller = self.emergency(fans=fans)
        self.patch_run()
        with self.assertLogs(_LOGGER, level="ERROR"):
            controller.run_cycle()
        self.assertEqual(fans.speeds, [100.0, 100.0, 100.0])


class DecisionLoggingTests(ControllerTestCase):
    """Logged on change only. A steady machine polls every few seconds forever;
    repeating an identical line would bury everything that matters."""

    def test_the_first_decision_is_logged(self):
        controller = self.build(_decision(speed=40.0))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
        self.assertTrue([line for line in logs.output if "40%" in line])

    def test_an_unchanged_decision_is_not_logged_again(self):
        controller = self.build(_decision(speed=40.0))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
            controller.run_cycle()
            controller.run_cycle()
        self.assertEqual(len(logs.output), 1)

    def test_a_changed_speed_is_logged(self):
        controller = self.build(_decision(speed=40.0))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
            controller._policy._decision = _decision(speed=70.0)
            controller.run_cycle()
        self.assertEqual(len(logs.output), 2)

    def test_a_change_reports_both_speeds(self):
        controller = self.build(_decision(speed=40.0))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
            controller._policy._decision = _decision(speed=70.0)
            controller.run_cycle()
        self.assertIn("40% -> 70%", logs.output[-1])

    def test_a_changed_mode_is_logged_even_at_the_same_speed(self):
        # WARNING at 100% and EMERGENCY at 100% are very different situations.
        controller = self.build(_decision(speed=100.0, mode=OperatingMode.WARNING))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
            controller._policy._decision = _decision(
                speed=100.0, mode=OperatingMode.EMERGENCY,
            )
            controller.run_cycle()
        self.assertEqual(len(logs.output), 2)
        self.assertIn("EMERGENCY", logs.output[-1])

    def test_the_mode_is_named_in_the_first_line(self):
        controller = self.build(_decision(speed=40.0, mode=OperatingMode.RUNNING))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
        self.assertIn("RUNNING", logs.output[0])

    def test_dry_run_is_marked_in_the_log(self):
        controller = self.build(_decision(speed=40.0), dry_run=True)
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
        self.assertIn("[dry-run]", logs.output[0])

    def test_a_live_run_is_not_marked(self):
        controller = self.build(_decision(speed=40.0))
        with self.assertLogs(_LOGGER, level="INFO") as logs:
            controller.run_cycle()
        self.assertNotIn("[dry-run]", logs.output[0])


if __name__ == "__main__":
    unittest.main()
