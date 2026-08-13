"""Tests for lib/managers/vm_manager.py.

The important property is that construction is inert: building the manager must
not touch a socket, or a host booting with one VM still down would never get
past setup() and would never cool anything.
"""

from __future__ import annotations

import unittest

from lib.managers.vm_manager import VMManager
from lib.models.vm import VMConfig
from lib.utils.qga import QGAClient


def _vm(name="vm1", socket=None) -> VMConfig:
    return VMConfig.from_dict(
        {
            "name": {"vm": name},
            "qga": {"socket": socket or f"/run/qemu/qemu-{name}-ga.sock"},
            "gpu": {"type": "nvidia"},
            "limits": {"max_temperature": 85},
        },
    )


class ClientTests(unittest.TestCase):
    def test_a_client_is_built_for_each_vm(self):
        manager = VMManager({"vm1": _vm("vm1"), "vm2": _vm("vm2")})
        self.assertIsInstance(manager.qga_client("vm1"), QGAClient)
        self.assertIsInstance(manager.qga_client("vm2"), QGAClient)

    def test_the_client_points_at_the_configured_socket(self):
        manager = VMManager({"vm1": _vm(socket="/run/qemu/custom.sock")})
        self.assertEqual(manager.qga_client("vm1")._socket_path, "/run/qemu/custom.sock")

    def test_the_same_client_is_returned_each_time(self):
        # One connection object per VM, reused across polls.
        manager = VMManager({"vm1": _vm()})
        self.assertIs(manager.qga_client("vm1"), manager.qga_client("vm1"))

    def test_each_vm_gets_its_own_client(self):
        manager = VMManager({"vm1": _vm("vm1"), "vm2": _vm("vm2")})
        self.assertIsNot(manager.qga_client("vm1"), manager.qga_client("vm2"))

    def test_an_unknown_vm_raises(self):
        manager = VMManager({"vm1": _vm()})
        with self.assertRaises(KeyError):
            manager.qga_client("nonexistent")

    def test_construction_opens_no_socket(self):
        # QGAClient connects per call, so a VM that is powered off costs
        # nothing at startup. If this ever changes, boot ordering breaks.
        manager = VMManager({"vm1": _vm(socket="/nonexistent/path/ga.sock")})
        self.assertIsInstance(manager.qga_client("vm1"), QGAClient)


class IterationTests(unittest.TestCase):
    def test_iteration_yields_name_and_config_pairs(self):
        vm = _vm("vm1")
        self.assertEqual(list(VMManager({"vm1": vm})), [("vm1", vm)])

    def test_every_vm_is_yielded(self):
        manager = VMManager({"vm1": _vm("vm1"), "vm2": _vm("vm2")})
        self.assertEqual({name for name, _ in manager}, {"vm1", "vm2"})

    def test_the_manager_can_be_iterated_more_than_once(self):
        # SensorManager.discover() re-iterates on every reload.
        manager = VMManager({"vm1": _vm()})
        self.assertEqual(list(manager), list(manager))

    def test_the_configuration_object_is_passed_through_unchanged(self):
        vm = _vm("vm1")
        (_name, yielded), = list(VMManager({"vm1": vm}))
        self.assertIs(yielded, vm)


class EmptyConfigurationTests(unittest.TestCase):
    """A host with no GPU guests is a supported deployment: IPMI sensors alone
    still drive the fans."""

    def test_no_vms_is_allowed(self):
        self.assertEqual(list(VMManager({})), [])

    def test_no_vms_still_raises_for_a_lookup(self):
        with self.assertRaises(KeyError):
            VMManager({}).qga_client("vm1")


if __name__ == "__main__":
    unittest.main()
