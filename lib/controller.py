"""Main orchestration loop.

Coordinates SensorManager -> State -> Policy -> Fan Controller each cycle.
Contains no hardware parsing, no configuration loading, and no safety/
emergency logic of its own — it only acts on what Policy returns.
"""

from __future__ import annotations

import subprocess

from lib.hardware.ipmi import IPMIError, IPMIFanController
from lib.managers.sensor_manager import SensorManager
from lib.policy import Policy
from lib.state import State
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
        self.state = State()

    def run_cycle(self) -> None:
        """One full control cycle: poll sensors, evaluate policy, apply
        the resulting fan speed, and act on a shutdown request if signaled.
        """
        self._sensor_manager.poll(self.state)
        decision = self._policy.evaluate(self.state)
        self._apply_fan_speed(decision.fan_speed_percent)
        if decision.shutdown_requested:
            self._shutdown_host()

    def _apply_fan_speed(self, percent: float) -> None:
        if self._dry_run:
            _log.info("[dry-run] would set fan speed to %.0f%%", percent)
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
            subprocess.run(["systemctl", "poweroff"], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            _log.error("failed to issue host shutdown: %s", exc)


@retry(exceptions=(IPMIError,), attempts=3, backoff=0.5)
def _set_speed_with_retry(fan_controller: IPMIFanController, percent: float) -> None:
    fan_controller.set_speed(percent)
