"""Tests for lib/managers/notification_manager.py.

Built from real NotifierConfig objects with the environment patched, so the
factory path runs for real. Home Assistant's URL points at a loopback server,
which makes self_test and delivery genuinely end to end.
"""

from __future__ import annotations

import os
import queue
import threading
import time
import unittest
from unittest.mock import patch

from lib.managers.notification_manager import NotificationManager
from lib.models.notification import NotifierConfig
from lib.state import OperatingMode, State
from tests.support.http_server import LoopbackServerTestCase

_ENV = {
    "FAND_DISCORD_TOKEN": "discord-token",
    "FAND_DISCORD_CHANNEL": "112233445566778899",
    "FAND_HOMEASSISTANT_URL": "https://ha.local:8123",
    "FAND_HOMEASSISTANT_TOKEN": "ha-token",
}


def _config(endpoint="discord", name="Test", interval=60, enabled=True, **trigger):
    credentials = (
        {"Token": "FAND_DISCORD_TOKEN", "Channel": "FAND_DISCORD_CHANNEL"}
        if endpoint == "discord"
        else {"URL": "FAND_HOMEASSISTANT_URL", "Token": "FAND_HOMEASSISTANT_TOKEN"}
    )
    return NotifierConfig.from_dict({
        "Name": name,
        "EndpointType": endpoint,
        "Enabled": enabled,
        "Interval": interval,
        "QueueSize": 10,
        "Trigger": trigger or {"Type": "general"},
        "Credentials": credentials,
    })


def _state(temperatures=(), mode=OperatingMode.RUNNING, fan=70.0,
           alarms=(), command_ok=None) -> State:
    state = State()
    for name, value in temperatures:
        state.temperatures[name] = type(
            "R", (), {"value": value, "timestamp": 1_700_000_000.0}
        )()
    state.mode = mode
    state.requested_fan_speed = fan
    state.alarms = set(alarms)
    if command_ok is not None:
        state.set_last_command_result(success=command_ok)
    return state


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, _ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Shorten the idle worker poll so stopping notifiers does not wait out
        # the production interval on every test. It governs shutdown
        # responsiveness only; no assertion here depends on its value.
        poll = patch("lib.notifications.notifier._QUEUE_POLL_SECONDS", 0.01)
        poll.start()
        self.addCleanup(poll.stop)

    def manager(self, configs=None, **kwargs) -> NotificationManager:
        manager = NotificationManager(configs or {}, **kwargs)
        self.addCleanup(manager.stop, 1.0)
        return manager

    @staticmethod
    def queued(manager, key) -> list:
        jobs = []
        while True:
            try:
                jobs.append(manager._notifiers[key]._queue.get_nowait())
            except queue.Empty:
                return jobs


class SnapshotTests(ManagerTestCase):
    def test_every_state_field_maps_across(self):
        state = _state(
            temperatures=[("CPU1 Temp", 80.0)], mode=OperatingMode.WARNING,
            fan=85.0, alarms=["over_temperature"], command_ok=True,
        )
        n = NotificationManager.build_notification(state)
        self.assertEqual(n.readings[0].name, "CPU1 Temp")
        self.assertEqual(n.readings[0].value_c, 80.0)
        self.assertEqual(n.operating_mode, "WARNING")
        self.assertEqual(n.fan_speed_percent, 85.0)
        self.assertEqual(n.alarms, ("over_temperature",))
        self.assertTrue(n.last_command_ok)

    def test_readings_are_sorted_by_name(self):
        # A sensor that fails and recovers is re-inserted at the end of the
        # dict, so insertion order drifts; sorting keeps output stable.
        state = _state(temperatures=[("n8n GPU", 91.0), ("CPU1 Temp", 80.0)])
        n = NotificationManager.build_notification(state)
        self.assertEqual(n.sensor_names, ("CPU1 Temp", "n8n GPU"))

    def test_alarms_are_sorted(self):
        # state.alarms is a set: iteration order is not stable across runs.
        state = _state(alarms=["z_alarm", "a_alarm", "m_alarm"])
        n = NotificationManager.build_notification(state)
        self.assertEqual(n.alarms, ("a_alarm", "m_alarm", "z_alarm"))

    def test_empty_state(self):
        n = NotificationManager.build_notification(State())
        self.assertEqual(n.readings, ())
        self.assertEqual(n.alarms, ())
        self.assertEqual(n.operating_mode, "STARTING")

    def test_absent_fan_speed_and_command_result_are_none(self):
        state = State()
        n = NotificationManager.build_notification(state)
        self.assertIsNone(n.fan_speed_percent)
        self.assertIsNone(n.last_command_ok)

    def test_snapshot_is_detached_from_state(self):
        state = _state(temperatures=[("CPU1 Temp", 80.0)])
        n = NotificationManager.build_notification(state)
        state.temperatures.clear()
        state.alarms.add("late")
        self.assertEqual(len(n.readings), 1)
        self.assertEqual(n.alarms, ())


class DispatchTests(ManagerTestCase):
    def test_no_notifiers_is_a_clean_noop(self):
        self.manager().dispatch(_state())

    def test_every_notifier_receives_a_job(self):
        manager = self.manager({"a": _config(), "b": _config(endpoint="homeassistant")})
        manager.dispatch(_state(temperatures=[("CPU1 Temp", 80.0)]))
        self.assertEqual(len(self.queued(manager, "a")), 1)
        self.assertEqual(len(self.queued(manager, "b")), 1)

    def test_all_notifiers_share_one_snapshot(self):
        manager = self.manager({"a": _config(), "b": _config(endpoint="homeassistant")})
        manager.dispatch(_state(temperatures=[("CPU1 Temp", 80.0)]))
        self.assertIs(self.queued(manager, "a")[0], self.queued(manager, "b")[0])

    def test_each_notifier_applies_its_own_trigger(self):
        configs = {
            "hot": _config(Type="threshold", Temperature=90),
            "always": _config(Type="general"),
        }
        manager = self.manager(configs)
        manager.dispatch(_state(temperatures=[("CPU1 Temp", 80.0)]))
        self.assertEqual(self.queued(manager, "hot"), [])
        self.assertEqual(len(self.queued(manager, "always")), 1)

    def test_one_failing_notifier_does_not_stop_the_others(self):
        manager = self.manager({"bad": _config(), "good": _config()})
        with patch.object(
            manager._notifiers["bad"], "offer", side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("lib.managers.notification_manager", level="ERROR"):
                manager.dispatch(_state(temperatures=[("CPU1 Temp", 80.0)]))
        self.assertEqual(len(self.queued(manager, "good")), 1)

    def test_dispatch_never_raises_when_the_snapshot_fails(self):
        manager = self.manager({"a": _config()})
        broken = State()
        del broken.temperatures  # force build_notification to fail
        with self.assertLogs("lib.managers.notification_manager", level="ERROR"):
            manager.dispatch(broken)

    def test_dispatch_performs_no_network_io(self):
        # It only queues; delivery is the worker's job.
        manager = self.manager({"a": _config()})
        with patch("lib.utils.http.post_json", side_effect=AssertionError("I/O!")):
            manager.dispatch(_state(temperatures=[("CPU1 Temp", 80.0)]))


class ReloadTests(ManagerTestCase):
    """The completion criterion: added, removed, edited, untouched."""

    def test_added_removed_edited_untouched(self):
        before = {
            "keep": _config(name="Keep"),
            "drop": _config(name="Drop"),
            "edit": _config(name="Edit", interval=60),
        }
        manager = self.manager(before)
        manager.start()
        untouched = manager._notifiers["keep"]
        retired = manager._notifiers["drop"]
        replaced = manager._notifiers["edit"]

        after = {
            "keep": _config(name="Keep"),
            "edit": _config(name="Edit", interval=30),
            "add": _config(name="Add"),
        }
        manager.reload(after)

        self.assertEqual(sorted(manager._notifiers), ["add", "edit", "keep"])
        # Untouched: the same object, still running.
        self.assertIs(manager._notifiers["keep"], untouched)
        self.assertIsNotNone(untouched._thread)
        # Edited: a different object.
        self.assertIsNot(manager._notifiers["edit"], replaced)
        # Removed and replaced: both stopped.
        self.assertIsNone(retired._thread)
        self.assertIsNone(replaced._thread)

    def test_untouched_notifier_keeps_its_queued_jobs(self):
        configs = {"keep": _config(name="Keep"), "other": _config(name="Other")}
        manager = self.manager(configs)
        manager.dispatch(_state(temperatures=[("CPU1 Temp", 80.0)]))
        self.assertEqual(manager._notifiers["keep"]._queue.qsize(), 1)

        manager.reload({"keep": _config(name="Keep")})
        # Queue survived the reload untouched.
        self.assertEqual(manager._notifiers["keep"]._queue.qsize(), 1)

    def test_disabling_retires_a_notifier(self):
        manager = self.manager({"a": _config(name="A")})
        manager.start()
        running = manager._notifiers["a"]
        manager.reload({"a": _config(name="A", enabled=False)})
        self.assertEqual(manager._notifiers, {})
        self.assertIsNone(running._thread)

    def test_disabled_notifiers_are_never_built(self):
        manager = self.manager({"off": _config(enabled=False)})
        self.assertEqual(manager._notifiers, {})

    def test_invalid_config_retires_the_running_notifier(self):
        manager = self.manager({"a": _config(name="A")})
        manager.start()
        running = manager._notifiers["a"]
        broken = _config(name="A")
        object.__setattr__(broken, "endpoint_type", "not-a-real-endpoint")
        with self.assertLogs("lib.managers.notification_manager", level="WARNING"):
            manager.reload({"a": broken})
        self.assertEqual(manager._notifiers, {})
        self.assertIsNone(running._thread)

    def test_a_bad_notifier_does_not_prevent_the_others_reloading(self):
        manager = self.manager({})
        broken = _config(name="Bad")
        object.__setattr__(broken, "endpoint_type", "nope")
        with self.assertLogs("lib.managers.notification_manager", level="WARNING"):
            manager.reload({"bad": broken, "good": _config(name="Good")})
        self.assertEqual(sorted(manager._notifiers), ["good"])

    def test_reload_to_nothing(self):
        manager = self.manager({"a": _config()})
        manager.start()
        manager.reload({})
        self.assertEqual(manager._notifiers, {})


class LifecycleTests(ManagerTestCase):
    def test_start_starts_every_notifier(self):
        manager = self.manager({"a": _config(), "b": _config()})
        manager.start()
        for notifier in manager._notifiers.values():
            self.assertIsNotNone(notifier._thread)

    def test_stop_stops_every_notifier(self):
        manager = self.manager({"a": _config(), "b": _config()})
        manager.start()
        notifiers = list(manager._notifiers.values())
        manager.stop(0.5)
        for notifier in notifiers:
            self.assertIsNone(notifier._thread)
        self.assertEqual(manager._notifiers, {})

    def test_stop_is_idempotent(self):
        manager = self.manager({"a": _config()})
        manager.start()
        manager.stop(0.5)
        manager.stop(0.5)

    def test_stop_on_an_empty_manager(self):
        self.manager().stop(0.5)


class StopTimingTests(ManagerTestCase):
    """Total shutdown must be bounded by one timeout, not N of them.

    That time is spent in Daemon.teardown, where a delay postpones the daemon
    releasing fan control back to iDRAC.
    """

    def test_several_wedged_notifiers_stop_in_about_one_timeout(self):
        released = threading.Event()
        self.addCleanup(released.set)

        def wedge(notification):
            released.wait(10.0)

        manager = self.manager({k: _config() for k in ("a", "b", "c", "d")})
        for notifier in manager._notifiers.values():
            notifier._endpoint.send = wedge
        manager.start()
        manager.dispatch(_state(temperatures=[("CPU1 Temp", 80.0)]))

        # Let every worker get stuck inside send().
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(n._queue.empty() for n in manager._notifiers.values()):
                break
            time.sleep(0.01)

        timeout = 0.3
        started = time.monotonic()
        manager.stop(timeout)
        elapsed = time.monotonic() - started
        # Four notifiers: serial joins would cost ~4x the timeout.
        self.assertLess(elapsed, timeout * 2.5)
        released.set()


class SelfTestTests(LoopbackServerTestCase):
    def setUp(self):
        super().setUp()
        env = {
            "FAND_HOMEASSISTANT_URL": self.origin,
            "FAND_HOMEASSISTANT_TOKEN": "ha-token",
        }
        patcher = patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _manager(self, **kwargs):
        manager = NotificationManager(
            {"ha": _config(endpoint="homeassistant", name="HA")}, **kwargs,
        )
        self.addCleanup(manager.stop, 1.0)
        return manager

    def test_reports_success(self):
        results = self._manager().self_test()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].notifier, "ha")
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].detail, "delivered")

    def test_actually_delivers(self):
        self._manager().self_test()
        self.assertTrue(self.requests)
        self.assertEqual(
            self.last_request["headers"]["authorization"], "Bearer ha-token",
        )

    def test_reports_failure_with_the_reason(self):
        self.respond(status=401)
        results = self._manager().self_test()
        self.assertFalse(results[0].ok)
        self.assertIn("401", results[0].detail)

    def test_dry_run_reports_skipped_not_passed(self):
        results = self._manager(dry_run=True).self_test()
        self.assertTrue(results[0].ok)
        self.assertIn("dry run", results[0].detail)
        self.assertEqual(self.requests, [])

    def test_empty_manager_reports_nothing(self):
        manager = NotificationManager({})
        self.addCleanup(manager.stop, 1.0)
        self.assertEqual(manager.self_test(), [])


if __name__ == "__main__":
    unittest.main()
