"""Main orchestration loop.

Coordinates SensorManager -> State -> Policy -> Fan Controller each cycle.
Contains no hardware parsing, no configuration loading, and no safety/
emergency logic of its own — it only acts on what Policy returns.
"""

from __future__ import annotations

import subprocess

from lib.hardware.ipmi import IPMIError, IPMIFanController
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
    ) -> None:
        self._sensor_manager = sensor_manager
        self._policy = policy
        self._fan_controller = fan_controller
        self._dry_run = dry_run
        self._last_logged_speed: float | None = None
        self._last_logged_mode: OperatingMode | None = None
        self.state = State()

    def run_cycle(self) -> None:
        """One full control cycle: poll sensors, evaluate policy, apply
        the resulting fan speed, and act on a shutdown request if signaled.
        """
        self._sensor_manager.poll(self.state)
        decision = self._policy.evaluate(self.state)
        self._log_fan_decision(decision)
        self._apply_fan_speed(decision.fan_speed_percent)
        if decision.shutdown_requested:
            self._shutdown_host()

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
        if self._last_logged_speed is None:
            _log.info(
                "%sFan speed set to %.0f%% (mode=%s)",
                prefix, decision.fan_speed_percent, decision.mode.name,
            )
        else:
            _log.info(
                "%sFan speed %.0f%% -> %.0f%% (mode=%s -> %s)",
                prefix, self._last_logged_speed, decision.fan_speed_percent,
                self._last_logged_mode.name, decision.mode.name,
            )
        self._last_logged_speed = decision.fan_speed_percent
        self._last_logged_mode = decision.mode

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
