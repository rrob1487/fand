"""Main orchestration loop.

Coordinates SensorManager -> State -> Policy -> Fan Controller each cycle.
Contains no hardware parsing, no configuration loading, and no safety/
emergency logic of its own — it only acts on what Policy returns.
"""

from __future__ import annotations

import subprocess

from lib.hardware.ipmi import IPMIError, IPMIFanController
from lib.managers.notification_manager import NotificationManager
from lib.managers.sensor_manager import SensorManager
from lib.policy import FanDecision, Policy
from lib.state import OperatingMode, State
from lib.utils.logging import get_logger
from lib.utils.retry import retry

_log = get_logger(__name__)


class Controller:
    def __init__(
        self,
        sensor_manager: SensorManager,
        policy: Policy,
        fan_controller: IPMIFanController,
        dry_run: bool = False,
        notification_manager: NotificationManager | None = None,
    ) -> None:
        self._sensor_manager = sensor_manager
        self._policy = policy
        self._fan_controller = fan_controller
        self._dry_run = dry_run
        self._notification_manager = notification_manager
        self._last_logged_speed: float | None = None
        self._last_logged_mode: OperatingMode | None = None
        self.state = State()

    def run_cycle(self) -> None:
        """One full control cycle: poll sensors, evaluate policy, apply
        the resulting fan speed, act on a shutdown request if signaled, and
        hand the resulting state to the notification subsystem.
        """
        self._sensor_manager.poll(self.state)
        decision = self._policy.evaluate(self.state)
        self._log_fan_decision(decision)
        if decision.automatic_control:
            self._apply_automatic_control()
        else:
            self._apply_fan_speed(decision.fan_speed_percent)
        if decision.shutdown_requested:
            self._shutdown_host()
        self._dispatch_notifications()

    def _dispatch_notifications(self) -> None:
        """Hand the current state to the notification subsystem, if any.

        Last in the cycle, so nothing sits between an emergency decision and
        the shutdown that answers it. Dispatch only evaluates triggers and
        queues jobs -- delivery happens on notifier worker threads -- so this
        performs no I/O and cannot delay the next poll or the watchdog.

        Guarded even though dispatch() already guards itself: notification is a
        non-critical subsystem embedded in a safety loop, and it must have no
        way to interrupt fan control.
        """
        if self._notification_manager is None:
            return
        try:
            self._notification_manager.dispatch(self.state)
        except Exception:
            _log.warning("notification dispatch failed", exc_info=True)

    def _log_fan_decision(self, decision: FanDecision) -> None:
        """Log at INFO (visible with or without -v) only when the fan
        target or operating mode actually changes, to avoid repeating an
        identical line every cycle on a steady system."""
        unchanged = (
            decision.fan_speed_percent == self._last_logged_speed
            and decision.mode == self._last_logged_mode
        )
        if unchanged:
            return

        prefix = "[dry-run] " if self._dry_run else ""
        if decision.automatic_control:
            # WARNING, not INFO: the daemon has stopped doing its job and the
            # machine is back on iDRAC's loud idle until someone looks.
            _log.warning(
                "%sSensor(s) lost for too long: %s; returning fan control to "
                "iDRAC automatic mode until they recover (mode=%s)",
                prefix, ", ".join(self.state.unmonitored_sensors) or "unknown",
                decision.mode.name,
            )
        elif self._last_logged_mode is None:
            _log.info(
                "%sFan speed set to %.0f%% (mode=%s)",
                prefix, decision.fan_speed_percent, decision.mode.name,
            )
        else:
            _log.info(
                "%sFan speed %s -> %.0f%% (mode=%s -> %s)",
                prefix, _format_speed(self._last_logged_speed),
                decision.fan_speed_percent,
                self._last_logged_mode.name, decision.mode.name,
            )
        self._last_logged_speed = decision.fan_speed_percent
        self._last_logged_mode = decision.mode

    def _apply_automatic_control(self) -> None:
        """Hand the fans to iDRAC while Policy says we cannot see enough.

        Re-asserted every cycle rather than once on entry: the command is
        idempotent, and a BMC that reset in the meantime would otherwise be
        left in whatever mode it came back up in. Leaving this mode needs no
        counterpart -- set_speed() re-enables manual control itself.
        """
        if self._dry_run:
            self.state.set_last_command_result(
                success=True, detail="[dry-run] would enable iDRAC automatic control",
            )
            return
        try:
            _enable_automatic_with_retry(self._fan_controller)
        except IPMIError as exc:
            _log.error("failed to enable automatic fan control: %s", exc)
            self.state.set_last_command_result(success=False, detail=str(exc))
        else:
            self.state.set_last_command_result(
                success=True, detail="iDRAC automatic control",
            )

    def _apply_fan_speed(self, percent: float) -> None:
        if self._dry_run:
            self.state.set_last_command_result(
                success=True, detail=f"[dry-run] would set to {percent:.0f}%",
            )
            return
        try:
            _set_speed_with_retry(self._fan_controller, percent)
        except IPMIError as exc:
            _log.error("failed to set fan speed to %.0f%%: %s", percent, exc)
            self.state.set_last_command_result(success=False, detail=str(exc))
        else:
            self.state.set_last_command_result(success=True, detail=f"set to {percent:.0f}%")

    def release_fan_control(self) -> None:
        """Hand fan control back to iDRAC automatic mode. Must not raise —
        called during teardown/shutdown, where a failure can't block exit."""
        if self._dry_run:
            _log.info("[dry-run] would release fan control to automatic mode")
            return
        try:
            self._fan_controller.enable_automatic_control()
        except IPMIError as exc:
            _log.error("failed to restore automatic fan control: %s", exc)
        else:
            _log.info("Released fan control to iDRAC automatic mode")

    def _shutdown_host(self) -> None:
        if self._dry_run:
            _log.info("[dry-run] would issue systemctl poweroff")
            return
        _log.critical("EMERGENCY shutdown requested by policy: issuing systemctl poweroff")
        try:
            # -n: fail immediately rather than hang on a password prompt
            # that will never come (this runs unattended).
            # --ignore-inhibitors: a thermal emergency must not be blocked
            # by an admin's forgotten SSH session.
            subprocess.run(
                ["sudo", "-n", "systemctl", "poweroff", "--ignore-inhibitors"], check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            _log.error("failed to issue host shutdown: %s", exc)


@retry(exceptions=(IPMIError,), attempts=3, backoff=0.5)
def _set_speed_with_retry(fan_controller: IPMIFanController, percent: float) -> None:
    fan_controller.set_speed(percent)


@retry(exceptions=(IPMIError,), attempts=3, backoff=0.5)
def _enable_automatic_with_retry(fan_controller: IPMIFanController) -> None:
    fan_controller.enable_automatic_control()


def _format_speed(percent: float | None) -> str:
    return "iDRAC auto" if percent is None else f"{percent:.0f}%"
