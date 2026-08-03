"""Maintains and polls the set of temperature sensors to monitor."""

from __future__ import annotations

from lib.factories.sensor_factory import create_gpu_sensor, discover_ipmi_sensors
from lib.hardware.ipmi import IPMI
from lib.hardware.sensor import Sensor
from lib.managers.vm_manager import VMManager
from lib.state import State
from lib.utils.logging import get_logger
from lib.utils.retry import retry

_log = get_logger(__name__)


class SensorManager:
    """Owns the sensor set and refreshes State from it each poll."""

    def __init__(self, vm_manager: VMManager, ipmi: IPMI) -> None:
        self._vm_manager = vm_manager
        self._ipmi = ipmi
        self._sensors: dict[str, Sensor] = {}
        self._failed_sensors: set[str] = set()

    def discover(self) -> None:
        """Rebuild the sensor set: one GPU sensor per VM plus every
        temperature sensor the BMC currently reports."""
        sensors: dict[str, Sensor] = {}
        for vm_name, vm in self._vm_manager:
            sensors[f"{vm_name} GPU"] = create_gpu_sensor(vm)
        sensors.update(discover_ipmi_sensors(self._ipmi))
        self._sensors = sensors

    def poll(self, state: State) -> None:
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


@retry(attempts=3, backoff=0.5)
def _read_sensor(sensor: Sensor) -> float:
    return sensor.read()
