"""Tests for lib/factories/sensor_factory.py.

Neither function decides temperature policy; they only build objects from what
configuration or the BMC report. The IPMI half matters most: sensor count and
naming vary by chassis, so the set is discovered from hardware rather than
hand-listed, and the keys have to survive that so a reading can be attributed
back to the sensor it came from.
"""

from __future__ import annotations

import unittest

from lib.factories.sensor_factory import create_gpu_sensor, discover_ipmi_sensors
from lib.hardware.gpu import GPUSensor
from lib.hardware.ipmi import IPMISensor
from lib.models.vm import VMConfig


def _vm(name="vm1", socket="/run/qemu/qemu-vm1-ga.sock") -> VMConfig:
    return VMConfig.from_dict(
        {
            "name": {"vm": name},
            "qga": {"socket": socket},
            "gpu": {"type": "nvidia"},
            "limits": {"max_temperature": 85},
        },
    )


class FakeIPMI:
    def __init__(self, *names: str):
        self._names = list(names)

    def temperature_sensor_names(self) -> list[str]:
        return list(self._names)


class GPUSensorTests(unittest.TestCase):
    def test_a_gpu_sensor_is_built(self):
        self.assertIsInstance(create_gpu_sensor(_vm()), GPUSensor)

    def test_the_sensor_is_wired_to_the_configured_socket(self):
        sensor = create_gpu_sensor(_vm(socket="/run/qemu/custom.sock"))
        self.assertEqual(sensor._qga._socket_path, "/run/qemu/custom.sock")

    def test_the_default_timeout_is_five_seconds(self):
        self.assertEqual(create_gpu_sensor(_vm())._timeout, 5.0)

    def test_the_timeout_can_be_overridden(self):
        self.assertEqual(create_gpu_sensor(_vm(), timeout=1.5)._timeout, 1.5)

    def test_construction_opens_no_socket(self):
        # Discovery runs at startup, when a guest may not be up yet.
        self.assertIsInstance(create_gpu_sensor(_vm(socket="/nonexistent.sock")), GPUSensor)

    def test_each_call_builds_a_separate_sensor(self):
        self.assertIsNot(create_gpu_sensor(_vm()), create_gpu_sensor(_vm()))


class IPMIDiscoveryTests(unittest.TestCase):
    def test_one_sensor_is_built_per_reported_name(self):
        sensors = discover_ipmi_sensors(FakeIPMI("Inlet Temp", "Exhaust Temp"))
        self.assertEqual(set(sensors), {"Inlet Temp", "Exhaust Temp"})

    def test_the_sensors_are_ipmi_sensors(self):
        sensors = discover_ipmi_sensors(FakeIPMI("Inlet Temp"))
        self.assertIsInstance(sensors["Inlet Temp"], IPMISensor)

    def test_each_sensor_reads_its_own_name(self):
        # The bug this prevents: every sensor wired to the same reading.
        sensors = discover_ipmi_sensors(FakeIPMI("Inlet Temp", "Exhaust Temp"))
        self.assertEqual(sensors["Inlet Temp"]._sensor_name, "Inlet Temp")
        self.assertEqual(sensors["Exhaust Temp"]._sensor_name, "Exhaust Temp")

    def test_duplicate_suffixed_names_are_preserved(self):
        # Dual-CPU boards report two sensors called "Temp"; the second arrives
        # as "Temp #2" and must stay addressable under that exact name.
        sensors = discover_ipmi_sensors(FakeIPMI("Temp", "Temp #2"))
        self.assertEqual(set(sensors), {"Temp", "Temp #2"})
        self.assertEqual(sensors["Temp #2"]._sensor_name, "Temp #2")

    def test_every_sensor_shares_the_one_ipmi_connection(self):
        ipmi = FakeIPMI("Inlet Temp", "Exhaust Temp")
        sensors = discover_ipmi_sensors(ipmi)
        for sensor in sensors.values():
            self.assertIs(sensor._ipmi, ipmi)

    def test_a_bmc_reporting_nothing_yields_no_sensors(self):
        # Survivable: Policy treats an empty reading set as an emergency and
        # runs the fans flat out, which is the safe direction.
        self.assertEqual(discover_ipmi_sensors(FakeIPMI()), {})

    def test_discovery_reads_no_temperatures(self):
        # Only the name list is queried; readings come later, during a poll.
        sensors = discover_ipmi_sensors(FakeIPMI("Inlet Temp"))
        self.assertEqual(len(sensors), 1)


if __name__ == "__main__":
    unittest.main()
