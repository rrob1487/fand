"""Tests for lib/hardware/sensor.py.

Small, but the contract matters: SensorManager polls anything shaped like a
Sensor, and sensor_factory hands it IPMISensor and GPUSensor interchangeably.
The ABC is what stops a half-written sensor source reaching the poll loop.
"""

from __future__ import annotations

import unittest

from lib.hardware.gpu import GPUSensor
from lib.hardware.ipmi import IPMISensor
from lib.hardware.sensor import Sensor


class InterfaceTests(unittest.TestCase):
    def test_the_interface_cannot_be_instantiated(self):
        with self.assertRaises(TypeError):
            Sensor()

    def test_a_subclass_without_read_cannot_be_instantiated(self):
        class Incomplete(Sensor):
            pass

        with self.assertRaises(TypeError):
            Incomplete()

    def test_a_subclass_implementing_read_can_be_instantiated(self):
        class Complete(Sensor):
            def read(self) -> float:
                return 42.0

        self.assertEqual(Complete().read(), 42.0)

    def test_read_is_declared_abstract(self):
        self.assertIn("read", Sensor.__abstractmethods__)


class ImplementationTests(unittest.TestCase):
    """The two shipped sources must stay substitutable, because SensorManager
    holds them in one dict and calls read() without knowing which is which."""

    def test_the_ipmi_sensor_implements_the_interface(self):
        self.assertTrue(issubclass(IPMISensor, Sensor))

    def test_the_gpu_sensor_implements_the_interface(self):
        self.assertTrue(issubclass(GPUSensor, Sensor))

    def test_neither_implementation_is_still_abstract(self):
        for implementation in (IPMISensor, GPUSensor):
            with self.subTest(implementation=implementation.__name__):
                self.assertEqual(implementation.__abstractmethods__, frozenset())


if __name__ == "__main__":
    unittest.main()
