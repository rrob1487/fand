"""Tests for lib/models/vm.py.

VM configuration is discovered from vms/*.toml rather than enumerated, so the
parsing here is what decides which guests get polled at all. Unlike notifier
configuration, a bad file is fatal: ConfigManager raises rather than skipping,
because a GPU nobody is watching is exactly the situation the daemon exists to
prevent.
"""

from __future__ import annotations

import pathlib
import tomllib
import unittest

from lib.models.vm import GPUMapping, QGAConnection, VMConfig, VMLimits

_CONFIG_DIR = pathlib.Path(__file__).resolve().parents[2] / "config"

_VALID = {
    "name": {"vm": "gpu-node-1"},
    "qga": {"socket": "/run/qemu/qemu-gpu-node-1-ga.sock"},
    "gpu": {"type": "nvidia"},
    "limits": {"max_temperature": 85},
}


def _vm(**overrides) -> dict:
    """_VALID with top-level tables replaced; a None value removes the table."""
    data = {k: (dict(v) if isinstance(v, dict) else v) for k, v in _VALID.items()}
    for key, value in overrides.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    return data


class SectionTests(unittest.TestCase):
    def test_the_socket_path_is_read(self):
        connection = QGAConnection.from_dict({"socket": "/run/qemu/ga.sock"})
        self.assertEqual(connection.socket, "/run/qemu/ga.sock")

    def test_the_socket_path_is_required(self):
        with self.assertRaises(KeyError):
            QGAConnection.from_dict({})

    def test_the_gpu_type_is_read(self):
        self.assertEqual(GPUMapping.from_dict({"type": "nvidia"}).type, "nvidia")

    def test_the_gpu_type_is_required(self):
        with self.assertRaises(KeyError):
            GPUMapping.from_dict({})

    def test_the_temperature_limit_is_read(self):
        self.assertEqual(VMLimits.from_dict({"max_temperature": 85}).max_temperature, 85)

    def test_the_temperature_limit_is_required(self):
        with self.assertRaises(KeyError):
            VMLimits.from_dict({})


class VMConfigTests(unittest.TestCase):
    def test_every_section_is_built(self):
        vm = VMConfig.from_dict(_vm())
        self.assertIsInstance(vm.qga, QGAConnection)
        self.assertIsInstance(vm.gpu, GPUMapping)
        self.assertIsInstance(vm.limits, VMLimits)

    def test_the_name_is_read_from_the_nested_table(self):
        # [name] vm = "..." rather than a bare top-level key.
        self.assertEqual(VMConfig.from_dict(_vm()).name, "gpu-node-1")

    def test_values_survive_the_round_trip(self):
        vm = VMConfig.from_dict(_vm())
        self.assertEqual(vm.qga.socket, "/run/qemu/qemu-gpu-node-1-ga.sock")
        self.assertEqual(vm.gpu.type, "nvidia")
        self.assertEqual(vm.limits.max_temperature, 85)

    def test_every_section_is_required(self):
        for section in ("name", "qga", "gpu", "limits"):
            with self.subTest(section=section):
                with self.assertRaises(KeyError):
                    VMConfig.from_dict(_vm(**{section: None}))

    def test_a_name_table_without_a_vm_key_raises(self):
        with self.assertRaises(KeyError):
            VMConfig.from_dict(_vm(name={}))

    def test_a_missing_nested_key_still_raises_key_error(self):
        with self.assertRaises(KeyError):
            VMConfig.from_dict(_vm(qga={}))


class ImmutabilityTests(unittest.TestCase):
    def test_the_config_is_frozen(self):
        vm = VMConfig.from_dict(_vm())
        with self.assertRaises(Exception):
            vm.name = "other"

    def test_the_socket_path_is_frozen(self):
        # VMManager builds a QGAClient from this at startup; repointing it
        # afterwards would silently leave the client on the old socket.
        vm = VMConfig.from_dict(_vm())
        with self.assertRaises(Exception):
            vm.qga.socket = "/run/qemu/elsewhere.sock"


class EqualityTests(unittest.TestCase):
    def test_identical_configs_compare_equal(self):
        self.assertEqual(VMConfig.from_dict(_vm()), VMConfig.from_dict(_vm()))

    def test_a_changed_socket_compares_unequal(self):
        other = _vm(qga={"socket": "/run/qemu/other.sock"})
        self.assertNotEqual(VMConfig.from_dict(_vm()), VMConfig.from_dict(other))


class ExampleFileTests(unittest.TestCase):
    """The shipped example must parse -- it is what operators copy."""

    def setUp(self):
        with open(_CONFIG_DIR / "vms" / "vm.toml.example", "rb") as handle:
            self.vm = VMConfig.from_dict(tomllib.load(handle))

    def test_the_example_parses(self):
        self.assertEqual(self.vm.name, "vm")
        self.assertEqual(self.vm.gpu.type, "nvidia")

    def test_the_example_points_at_a_guest_agent_socket(self):
        self.assertTrue(self.vm.qga.socket.startswith("/"))
        self.assertIn("ga.sock", self.vm.qga.socket)

    def test_the_example_socket_is_reachable_under_the_units_writable_paths(self):
        # fand.service grants ReadWritePaths=/opt/fand /run/fand /run/qemu, so
        # an example pointing anywhere else would not work as copied.
        self.assertTrue(self.vm.qga.socket.startswith("/run/qemu/"))

    def test_the_example_sets_a_gpu_temperature_limit(self):
        self.assertGreater(self.vm.limits.max_temperature, 0)


if __name__ == "__main__":
    unittest.main()
