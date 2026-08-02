"""Creates hardware Sensor implementations from configuration/hardware.

Neither function here decides temperature policy — they only construct
objects from what configuration or the BMC report.
"""

from __future__ import annotations

from lib.hardware.gpu import GPUSensor
from lib.hardware.ipmi import IPMI, IPMISensor
from lib.models.vm import VMConfig
from lib.utils.qga import QGAClient


def create_gpu_sensor(vm: VMConfig, timeout: float = 5.0) -> GPUSensor:
    """Build a GPUSensor for a VM from its configuration."""
    return GPUSensor(QGAClient(vm.qga.socket), timeout=timeout)


def discover_ipmi_sensors(ipmi: IPMI) -> dict[str, IPMISensor]:
    """Build one IPMISensor per temperature sensor the BMC currently reports.

    IPMI temperature sensor count and naming vary by chassis (e.g. dual-CPU
    boards report two sensors both named "Temp"), so sensors are discovered
    from hardware rather than hand-enumerated in configuration. Keyed by
    name so callers can attribute readings back to their source.
    """
    return {name: IPMISensor(ipmi, name) for name in ipmi.temperature_sensor_names()}
