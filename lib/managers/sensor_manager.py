"""Maintains and polls the set of temperature sensors to monitor."""

from __future__ import annotations

import time

from lib.factories.sensor_factory import create_gpu_sensor, discover_ipmi_sensors
from lib.hardware.ipmi import IPMI
from lib.hardware.sensor import Sensor
from lib.managers.vm_manager import VMManager
from lib.state import State
from lib.utils.logging import get_logger
from lib.utils.retry import retry

_log = get_logger(__name__)

_DEFAULT_REDISCOVER_INTERVAL_SECONDS = 300.0


class SensorManager:
    """Owns the sensor set and refreshes State from it each poll."""

    def __init__(
        self,
        vm_manager: VMManager,
        ipmi: IPMI,
        rediscover_interval: float = _DEFAULT_REDISCOVER_INTERVAL_SECONDS,
    ) -> None:
        self._vm_manager = vm_manager
        self._ipmi = ipmi
        self._rediscover_interval = rediscover_interval
        self._sensors: dict[str, Sensor] = {}
        self._failed_sensors: set[str] = set()
        # Started now, not at zero: a manager that has just been built is not
        # overdue for a re-scan, and the daemon calls discover() itself.
        self._last_discovery = time.monotonic()

    def discover(self) -> None:
        """Rebuild the sensor set: one GPU sensor per VM plus every
        temperature sensor the BMC currently reports."""
        self._sensors = self._build_sensors()
        self._last_discovery = time.monotonic()
        _log.info(
            "discovered %d sensor(s): %s",
            len(self._sensors), ", ".join(self._sensors) or "none",
        )

    def _build_sensors(self) -> dict[str, Sensor]:
        sensors: dict[str, Sensor] = {}
        for vm_name, vm in self._vm_manager:
            sensors[f"{vm_name} GPU"] = create_gpu_sensor(vm)
        sensors.update(discover_ipmi_sensors(self._ipmi))
        return sensors

    def poll(self, state: State) -> None:
        # One BMC query per cycle instead of one per sensor, and every reading
        # below comes from the same sample.
        self._ipmi.begin_cycle()
        self._maybe_rediscover(state)

        for name, sensor in self._sensors.items():
            try:
                value = _read_sensor(sensor)
            except Exception as exc:
                if name not in self._failed_sensors:
                    _log.warning("sensor %r lost: %s", name, exc)
                    self._failed_sensors.add(name)
                state.clear_temperature(name)
                state.set_alarm(f"sensor_failure:{name}")
                continue
            if name in self._failed_sensors:
                _log.info("sensor %r recovered", name)
                self._failed_sensors.discard(name)
            state.update_temperature(name, value)
            state.clear_alarm(f"sensor_failure:{name}")
            _log.debug("sensor %r = %.1f", name, value)

    def _maybe_rediscover(self, state: State) -> None:
        """Re-scan the sensor set, if it is due.

        The set is not fixed at startup. A BMC that is still initialising --
        after an AC power loss, say -- reports a short SDR, and a set discovered
        once would leave the daemon driving a fraction of its sensors until
        someone restarted it, with nothing in the log to say so.
        """
        now = time.monotonic()
        if now - self._last_discovery < self._rediscover_interval:
            return
        self._last_discovery = now

        try:
            rediscovered = self._build_sensors()
        except Exception as exc:
            # Keeping the previous set is the safe failure. Policy reads an
            # empty temperature set as "no data" and answers with EMERGENCY and
            # 100% fans, so letting a transient ipmitool failure empty the set
            # would turn a blip into a chassis running every fan flat out.
            _log.warning("sensor re-scan failed, keeping the previous sensors: %s", exc)
            return

        added = [name for name in rediscovered if name not in self._sensors]
        removed = [name for name in self._sensors if name not in rediscovered]
        if not added and not removed:
            # Re-logging an identical set every interval would bury the scan
            # that actually changed something.
            return

        # Sensors that were already present keep their existing object: a
        # re-scan is not a reason to discard whatever state one of them holds.
        self._sensors = {
            name: self._sensors.get(name, sensor)
            for name, sensor in rediscovered.items()
        }
        for name in removed:
            self._failed_sensors.discard(name)
            state.clear_temperature(name)
            state.clear_alarm(f"sensor_failure:{name}")

        if added:
            _log.info("sensors appeared: %s", ", ".join(added))
        if removed:
            _log.info("sensors disappeared: %s", ", ".join(removed))


@retry(attempts=3, backoff=0.5)
def _read_sensor(sensor: Sensor) -> float:
    return sensor.read()
