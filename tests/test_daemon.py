"""Tests for lib/daemon.py, focused on notification integration.

IPMI is patched so no ipmitool runs; configuration comes from a real temporary
directory.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.daemon import Daemon
from lib.managers.config_manager import ConfigError

_CONFIG_TOML = """
[daemon]
poll_interval = 5
log_level = "INFO"

[fan_curve]
points = [[40, 20], [85, 100]]

[safety]
max_temperature = 90

[watchdog]
enabled = true
"""

_ENV = {
    "FAND_DISCORD_TOKEN": "token",
    "FAND_DISCORD_CHANNEL": "channel",
}


def _notifier_toml(name="Test") -> str:
    return f"""
Name = "{name}"
EndpointType = "discord"
Interval = 60
QueueSize = 10

[Trigger]
Type = "general"

[Credentials]
Token = "FAND_DISCORD_TOKEN"
Channel = "FAND_DISCORD_CHANNEL"
"""


class FakeIPMI:
    def __init__(self, *args, **kwargs):
        pass

    def temperature_sensor_names(self):
        return []


class DaemonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.write("config.toml", _CONFIG_TOML)

        for target in ("lib.daemon.IPMI", "lib.daemon.IPMIFanController"):
            patcher = patch(target, FakeIPMI)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", _ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)
        poll = patch("lib.notifications.notifier._QUEUE_POLL_SECONDS", 0.01)
        poll.start()
        self.addCleanup(poll.stop)

    def write(self, relative: str, text: str) -> None:
        path = self.dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def daemon(self, **kwargs) -> Daemon:
        daemon = Daemon(config_dir=self.dir, **kwargs)
        self.addCleanup(daemon.teardown)
        return daemon


class SignalHandlerTests(DaemonTestCase):
    """The deadlock hazard: a handler that logs can re-enter logging while the
    module lock is already held, on a daemon whose worker threads log
    constantly."""

    def test_shutdown_handler_only_sets_flags(self):
        daemon = self.daemon()
        with self.assertNoLogs("fand", level="DEBUG"):
            daemon._handle_shutdown_signal(15, None)
        self.assertTrue(daemon._shutdown_requested)
        self.assertEqual(daemon._shutdown_signal, 15)

    def test_reload_handler_only_sets_a_flag(self):
        daemon = self.daemon()
        with self.assertNoLogs("fand", level="DEBUG"):
            daemon._handle_reload_signal(1, None)
        self.assertTrue(daemon._reload_requested)

    def test_reload_handler_does_not_reload(self):
        # The work happens in run(), not in the handler.
        daemon = self.daemon()
        daemon.setup()
        with patch.object(daemon, "reload_config") as reload_config:
            daemon._handle_reload_signal(1, None)
        reload_config.assert_not_called()


class NotificationLifecycleTests(DaemonTestCase):
    def test_manager_is_created_and_started_by_setup(self):
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon()
        daemon.setup()
        self.assertIsNotNone(daemon._notification_manager)
        notifier = daemon._notification_manager._notifiers["a"]
        self.assertIsNotNone(notifier._thread)

    def test_controller_receives_the_manager(self):
        daemon = self.daemon()
        daemon.setup()
        self.assertIs(
            daemon._controller._notification_manager, daemon._notification_manager,
        )

    def test_manager_survives_a_reload(self):
        """Created outside _build_controller precisely so a SIGHUP does not
        respawn every worker and discard every queued job."""
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon()
        daemon.setup()
        manager = daemon._notification_manager
        notifier = manager._notifiers["a"]
        controller = daemon._controller

        daemon.reload_config()

        self.assertIs(daemon._notification_manager, manager)
        self.assertIs(manager._notifiers["a"], notifier)   # untouched config
        self.assertIsNot(daemon._controller, controller)   # rebuilt, as before

    def test_startup_survives_a_broken_notification_subsystem(self):
        with patch(
            "lib.daemon.NotificationManager", side_effect=RuntimeError("boom"),
        ):
            daemon = self.daemon()
            with self.assertLogs("fand", level="ERROR"):
                daemon.setup()
        self.assertIsNone(daemon._notification_manager)
        self.assertIsNotNone(daemon._controller)


class ReloadTests(DaemonTestCase):
    def test_notifiers_are_reloaded(self):
        daemon = self.daemon()
        daemon.setup()
        self.assertEqual(daemon._notification_manager._notifiers, {})
        self.write("notification/new.toml", _notifier_toml("New"))
        daemon.reload_config()
        self.assertIn("new", daemon._notification_manager._notifiers)

    def test_a_notifier_reload_failure_does_not_undo_the_config_reload(self):
        daemon = self.daemon()
        daemon.setup()
        self.write("config.toml", _CONFIG_TOML.replace("poll_interval = 5", "poll_interval = 7"))
        with patch.object(
            daemon._notification_manager, "reload", side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("fand", level="ERROR"):
                daemon.reload_config()
        self.assertEqual(daemon._config_manager.config.daemon.poll_interval, 7)

    def test_a_bad_config_keeps_the_previous_one(self):
        daemon = self.daemon()
        daemon.setup()
        self.write("config.toml", "not [ valid toml")
        with self.assertLogs("fand", level="ERROR"):
            daemon.reload_config()
        self.assertEqual(daemon._config_manager.config.daemon.poll_interval, 5)

    def test_a_bad_config_does_not_reach_the_notifier_reload(self):
        daemon = self.daemon()
        daemon.setup()
        self.write("config.toml", "not [ valid toml")
        with patch.object(daemon._notification_manager, "reload") as reload_notifiers:
            with self.assertLogs("fand", level="ERROR"):
                daemon.reload_config()
        reload_notifiers.assert_not_called()


class TeardownOrderingTests(DaemonTestCase):
    """CLAUDE.md's third non-negotiable: daemon exit always releases fan
    control. Notification shutdown must be incapable of interfering."""

    def _instrumented(self, release_error=None, stop_error=None):
        daemon = self.daemon()
        daemon.setup()
        log = []

        def release():
            log.append("release_fan_control")
            if release_error:
                raise release_error

        def stop(timeout=2.0):
            log.append("stop_notifications")
            if stop_error:
                raise stop_error

        daemon._controller.release_fan_control = release
        daemon._notification_manager.stop = stop
        return daemon, log

    def test_fan_control_is_released_before_notifications_stop(self):
        daemon, log = self._instrumented()
        daemon.teardown()
        self.assertEqual(log, ["release_fan_control", "stop_notifications"])

    def test_a_failing_notification_stop_still_leaves_fans_released(self):
        daemon, log = self._instrumented(stop_error=RuntimeError("wedged"))
        with self.assertLogs("fand", level="ERROR"):
            daemon.teardown()
        self.assertEqual(log, ["release_fan_control", "stop_notifications"])

    def test_a_failing_fan_release_still_stops_notifications(self):
        daemon, log = self._instrumented(release_error=RuntimeError("ipmi gone"))
        with self.assertLogs("fand", level="ERROR"):
            daemon.teardown()
        self.assertIn("stop_notifications", log)

    def test_teardown_without_a_notification_manager(self):
        daemon = self.daemon()
        daemon.setup()
        daemon._notification_manager = None
        daemon.teardown()


class NotifyTestModeTests(DaemonTestCase):
    def test_returns_zero_when_nothing_is_configured(self):
        # Nothing failed, so the exit code should not say otherwise.
        daemon = self.daemon()
        with self.assertLogs("fand", level="WARNING") as logs:
            self.assertEqual(daemon.run_notify_test(), 0)
        self.assertTrue([x for x in logs.output if "no enabled notifiers" in x])

    def test_returns_one_when_a_notifier_fails(self):
        # discord.com is unreachable from the test environment; the point is
        # that the failure is reported rather than raised.
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon()
        with patch(
            "lib.notifications.discord.post_json", side_effect=RuntimeError("no network"),
        ):
            with self.assertLogs("fand", level="ERROR"):
                self.assertEqual(daemon.run_notify_test(), 1)

    def test_returns_zero_when_every_notifier_succeeds(self):
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon()
        with patch("lib.notifications.discord.post_json") as post:
            post.return_value = type("R", (), {"status": 200, "retry_after": None})()
            self.assertEqual(daemon.run_notify_test(), 0)

    def test_touches_no_ipmi(self):
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon()
        with patch("lib.daemon.IPMI", side_effect=AssertionError("IPMI touched!")):
            with patch("lib.notifications.discord.post_json") as post:
                post.return_value = type("R", (), {"status": 200, "retry_after": None})()
                daemon.run_notify_test()

    def test_starts_no_worker_threads(self):
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon()
        with patch("lib.notifications.discord.post_json") as post:
            post.return_value = type("R", (), {"status": 200, "retry_after": None})()
            daemon.run_notify_test()
        self.assertIsNone(daemon._notification_manager)

    def test_dry_run_reports_skipped_and_sends_nothing(self):
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon(dry_run=True)
        with patch(
            "lib.notifications.discord.post_json", side_effect=AssertionError("sent!"),
        ):
            self.assertEqual(daemon.run_notify_test(), 0)

    def test_a_notifier_that_cannot_be_built_counts_as_a_failure(self):
        # Reporting a pass because it never ran would defeat the point of
        # asking for a test.
        self.write("notification/a.toml", _notifier_toml())
        daemon = self.daemon()
        with patch.dict("os.environ", {"FAND_DISCORD_TOKEN": ""}):
            with self.assertLogs("fand", level="ERROR") as logs:
                self.assertEqual(daemon.run_notify_test(), 1)
        self.assertTrue([x for x in logs.output if "could not be created" in x])


if __name__ == "__main__":
    unittest.main()
