"""Tests for lib/daemon.py: lifecycle, the main loop, and systemd integration.

IPMI is patched so no ipmitool runs; configuration comes from a real temporary
directory.

The sd_notify and signal-handler tests need AF_UNIX and SIGHUP, which do not
exist on Windows, so they are skipped there and run for real on the target host.
Everything else -- including the whole main loop -- runs on any platform, with
install_signal_handlers patched out so the loop never touches process-global
handlers or a signal number that may not exist.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.daemon import Daemon, SdNotifier

_POSIX_ONLY = unittest.skipUnless(
    hasattr(socket, "AF_UNIX"), "AF_UNIX sockets are POSIX-only",
)
_HAS_SIGHUP = unittest.skipUnless(
    hasattr(signal, "SIGHUP"), "SIGHUP does not exist on Windows",
)

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


class FakeSdNotifier:
    """Records what would have gone to systemd."""

    def __init__(self):
        self.sent: list[str] = []

    def ready(self):
        self.sent.append("READY=1")

    def stopping(self):
        self.sent.append("STOPPING=1")

    def watchdog(self):
        self.sent.append("WATCHDOG=1")

    def status(self, text):
        self.sent.append(f"STATUS={text}")


class DaemonTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.write("config.toml", _CONFIG_TOML)

        # setup() and reload_config() both call set_level, which rewrites the
        # root logger's level for the whole process. Put it back so a daemon
        # test cannot change what later tests in the run are able to see.
        root = logging.getLogger()
        self.addCleanup(root.setLevel, root.level)

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


class SetupTests(DaemonTestCase):
    def test_the_poll_interval_falls_back_to_the_config_file(self):
        daemon = self.daemon()
        daemon.setup()
        self.assertEqual(daemon.poll_interval, 5)

    def test_the_command_line_poll_interval_wins(self):
        daemon = self.daemon(poll_interval=1.5)
        daemon.setup()
        self.assertEqual(daemon.poll_interval, 1.5)

    def test_a_zero_poll_interval_is_not_treated_as_unset(self):
        daemon = self.daemon(poll_interval=0)
        daemon.setup()
        self.assertEqual(daemon.poll_interval, 0)

    def test_setup_builds_a_controller(self):
        daemon = self.daemon()
        daemon.setup()
        self.assertIsNotNone(daemon._controller)

    def test_a_missing_config_file_is_fatal(self):
        # The opposite of a bad notifier file: without a fan curve the machine
        # cannot be cooled, so startup must stop rather than limp on.
        daemon = Daemon(config_dir=self.dir / "nonexistent")
        with self.assertRaises(Exception):
            daemon.setup()


class LogLevelTests(DaemonTestCase):
    def test_the_configured_level_is_applied(self):
        daemon = self.daemon()
        with patch("lib.daemon.set_level") as set_level:
            daemon.setup()
        set_level.assert_called_once_with("INFO")

    def test_verbose_overrides_the_config_file(self):
        # -v is an explicit operator instruction and always wins.
        daemon = self.daemon(verbose=True)
        with patch("lib.daemon.set_level") as set_level:
            daemon.setup()
        set_level.assert_not_called()

    def test_an_invalid_level_warns_and_keeps_the_current_one(self):
        self.write("config.toml", _CONFIG_TOML.replace('"INFO"', '"VERBOSE"'))
        daemon = self.daemon()
        before = logging.getLogger().level
        with self.assertLogs("fand", level="WARNING") as logs:
            daemon.setup()
        self.assertTrue([line for line in logs.output if "invalid config log_level" in line])
        self.assertEqual(logging.getLogger().level, before)

    def test_an_invalid_level_does_not_stop_startup(self):
        # A typo in a log level must never keep the fans from being driven.
        self.write("config.toml", _CONFIG_TOML.replace('"INFO"', '"VERBOSE"'))
        daemon = self.daemon()
        with self.assertLogs("fand", level="WARNING"):
            daemon.setup()
        self.assertIsNotNone(daemon._controller)


class WatchdogTests(DaemonTestCase):
    """systemd restarts a daemon that stops pinging. The interval is half the
    unit's WatchdogSec, so a single slow poll cannot trip it."""

    def setUp(self):
        super().setUp()
        self.clock = 1000.0
        patcher = patch("lib.daemon.time.monotonic", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, usec=None) -> Daemon:
        values = {"WATCHDOG_USEC": usec} if usec is not None else {}
        with patch.dict(os.environ, values, clear=False):
            if usec is None:
                os.environ.pop("WATCHDOG_USEC", None)
            daemon = self.daemon()   # the interval is read in __init__
        daemon.notifier = FakeSdNotifier()
        return daemon

    def test_no_watchdog_configured_means_no_pings(self):
        daemon = self.build()
        self.assertIsNone(daemon._watchdog_interval)
        daemon.maybe_ping_watchdog()
        self.assertEqual(daemon.notifier.sent, [])

    def test_the_interval_is_half_the_systemd_timeout(self):
        # Half, so one missed cycle still leaves a full interval of margin.
        daemon = self.build("60000000")
        self.assertEqual(daemon._watchdog_interval, 30.0)

    def test_a_ping_is_sent_once_the_interval_has_elapsed(self):
        daemon = self.build("60000000")
        daemon._last_watchdog_ping = self.clock
        self.clock += 30.0
        daemon.maybe_ping_watchdog()
        self.assertEqual(daemon.notifier.sent, ["WATCHDOG=1"])

    def test_no_ping_is_sent_before_the_interval_elapses(self):
        daemon = self.build("60000000")
        daemon._last_watchdog_ping = self.clock
        self.clock += 29.9
        daemon.maybe_ping_watchdog()
        self.assertEqual(daemon.notifier.sent, [])

    def test_pings_are_not_repeated_within_one_interval(self):
        daemon = self.build("60000000")
        daemon._last_watchdog_ping = self.clock
        self.clock += 30.0
        daemon.maybe_ping_watchdog()
        daemon.maybe_ping_watchdog()
        self.assertEqual(daemon.notifier.sent, ["WATCHDOG=1"])

    def test_pings_resume_after_the_next_interval(self):
        daemon = self.build("60000000")
        daemon._last_watchdog_ping = self.clock
        for _ in range(3):
            self.clock += 30.0
            daemon.maybe_ping_watchdog()
        self.assertEqual(daemon.notifier.sent, ["WATCHDOG=1"] * 3)


class RunLoopTests(DaemonTestCase):
    """One bad iteration must never kill the daemon, and every exit path must
    reach teardown -- CLAUDE.md non-negotiable 3."""

    def setUp(self):
        super().setUp()
        sleep = patch("lib.daemon.time.sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def build(self, iterations=3, work=None, **kwargs) -> Daemon:
        daemon = self.daemon(poll_interval=0, **kwargs)
        # install_signal_handlers is platform-specific and process-global; it
        # has its own tests below.
        signals = patch.object(daemon, "install_signal_handlers")
        signals.start()
        self.addCleanup(signals.stop)

        self.work: list[int] = []
        self.teardowns: list[str] = []
        real_teardown = daemon.teardown

        def do_work():
            self.work.append(len(self.work))
            # The stop condition is set before the injected work runs, so a
            # test whose work raises still terminates the loop.
            if len(self.work) >= iterations:
                daemon._shutdown_requested = True
            if work is not None:
                work(daemon, len(self.work))

        def teardown():
            self.teardowns.append("teardown")
            real_teardown()

        daemon.do_work = do_work
        daemon.teardown = teardown
        daemon.notifier = FakeSdNotifier()
        return daemon

    def test_the_loop_runs_until_the_shutdown_flag_is_set(self):
        daemon = self.build(iterations=3)
        self.assertEqual(daemon.run(), 0)
        self.assertEqual(len(self.work), 3)

    def test_a_clean_exit_returns_zero(self):
        self.assertEqual(self.build().run(), 0)

    def test_teardown_runs_on_a_clean_exit(self):
        self.build().run()
        self.assertEqual(self.teardowns, ["teardown"])

    def test_systemd_is_told_the_daemon_is_ready(self):
        daemon = self.build()
        daemon.run()
        self.assertIn("READY=1", daemon.notifier.sent)

    def test_systemd_is_told_the_daemon_is_stopping(self):
        daemon = self.build()
        daemon.run()
        self.assertIn("STOPPING=1", daemon.notifier.sent)

    def test_ready_is_sent_before_stopping(self):
        daemon = self.build()
        daemon.run()
        sent = daemon.notifier.sent
        self.assertLess(sent.index("READY=1"), sent.index("STOPPING=1"))

    def test_the_loop_sleeps_for_the_poll_interval(self):
        daemon = self.build(iterations=2)
        daemon.poll_interval = 4.0
        daemon.run()
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [4.0, 4.0])

    def test_a_failing_iteration_is_logged_and_the_loop_continues(self):
        def explode(daemon, count):
            if count == 1:
                raise RuntimeError("BMC fell over")

        daemon = self.build(iterations=3, work=explode)
        with self.assertLogs("fand", level="ERROR") as logs:
            self.assertEqual(daemon.run(), 0)
        self.assertEqual(len(self.work), 3)
        self.assertTrue([line for line in logs.output if "Critical hardware failure" in line])

    def test_every_iteration_failing_still_exits_cleanly(self):
        def always_explode(daemon, count):
            raise RuntimeError("BMC gone")

        daemon = self.build(iterations=3, work=always_explode)
        with self.assertLogs("fand", level="ERROR"):
            self.assertEqual(daemon.run(), 0)
        self.assertEqual(self.teardowns, ["teardown"])

    def test_a_fatal_error_returns_one(self):
        daemon = self.build()
        with patch.object(daemon, "maybe_ping_watchdog", side_effect=RuntimeError("boom")):
            with self.assertLogs("fand", level="ERROR"):
                self.assertEqual(daemon.run(), 1)

    def test_a_fatal_error_still_reaches_teardown(self):
        # Where the fans get handed back to iDRAC. Skipping it would leave them
        # pinned under manual control with nothing driving them.
        daemon = self.build()
        with patch.object(daemon, "maybe_ping_watchdog", side_effect=RuntimeError("boom")):
            with self.assertLogs("fand", level="ERROR"):
                daemon.run()
        self.assertEqual(self.teardowns, ["teardown"])

    def test_a_fatal_error_still_tells_systemd_it_is_stopping(self):
        daemon = self.build()
        with patch.object(daemon, "maybe_ping_watchdog", side_effect=RuntimeError("boom")):
            with self.assertLogs("fand", level="ERROR"):
                daemon.run()
        self.assertIn("STOPPING=1", daemon.notifier.sent)

    def test_a_shutdown_signal_is_reported_on_the_way_out(self):
        def signalled(daemon, count):
            daemon._shutdown_signal = 15

        daemon = self.build(iterations=1, work=signalled)
        with self.assertLogs("fand", level="INFO") as logs:
            daemon.run()
        self.assertTrue([line for line in logs.output if "shutting down gracefully" in line])

    def test_teardown_runs_after_a_signalled_shutdown(self):
        def signalled(daemon, count):
            daemon._shutdown_signal = 15
            daemon._shutdown_requested = True

        self.build(iterations=99, work=signalled).run()
        self.assertEqual(self.teardowns, ["teardown"])

    def test_a_shutdown_flag_set_before_run_skips_the_loop_entirely(self):
        daemon = self.build()
        daemon._shutdown_requested = True
        self.assertEqual(daemon.run(), 0)
        self.assertEqual(self.work, [])
        self.assertEqual(self.teardowns, ["teardown"])


class ReloadSignalTests(DaemonTestCase):
    """SIGHUP sets a flag; run() is where the reload actually happens."""

    def setUp(self):
        super().setUp()
        sleep = patch("lib.daemon.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def build(self, work=None, iterations=3) -> Daemon:
        daemon = self.daemon(poll_interval=0)
        signals = patch.object(daemon, "install_signal_handlers")
        signals.start()
        self.addCleanup(signals.stop)
        self.work: list[int] = []

        def do_work():
            self.work.append(len(self.work))
            # The stop condition is set before the injected work runs, so a
            # test whose work raises still terminates the loop.
            if len(self.work) >= iterations:
                daemon._shutdown_requested = True
            if work is not None:
                work(daemon, len(self.work))

        daemon.do_work = do_work
        daemon.notifier = FakeSdNotifier()
        return daemon

    def test_the_reload_flag_triggers_one_reload(self):
        def request_reload(daemon, count):
            if count == 1:
                daemon._reload_requested = True

        daemon = self.build(work=request_reload)
        with patch.object(daemon, "reload_config") as reload_config:
            daemon.run()
        self.assertEqual(reload_config.call_count, 1)

    def test_the_reload_flag_is_cleared(self):
        def request_reload(daemon, count):
            if count == 1:
                daemon._reload_requested = True

        daemon = self.build(work=request_reload)
        with patch.object(daemon, "reload_config"):
            daemon.run()
        self.assertFalse(daemon._reload_requested)

    def test_no_reload_happens_without_the_flag(self):
        daemon = self.build()
        with patch.object(daemon, "reload_config") as reload_config:
            daemon.run()
        reload_config.assert_not_called()

    def test_a_reload_is_announced(self):
        def request_reload(daemon, count):
            if count == 1:
                daemon._reload_requested = True

        daemon = self.build(work=request_reload)
        with patch.object(daemon, "reload_config"):
            with self.assertLogs("fand", level="INFO") as logs:
                daemon.run()
        self.assertTrue([line for line in logs.output if "Received SIGHUP" in line])

    def test_two_reloads_are_both_honoured(self):
        def request_reload(daemon, count):
            if count in (1, 2):
                daemon._reload_requested = True

        daemon = self.build(work=request_reload, iterations=4)
        with patch.object(daemon, "reload_config") as reload_config:
            daemon.run()
        self.assertEqual(reload_config.call_count, 2)


@_HAS_SIGHUP
class SignalRegistrationTests(DaemonTestCase):
    """Registering the handlers for real. Process-global, so the previous
    handlers are captured and restored around every test."""

    def setUp(self):
        super().setUp()
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            number = getattr(signal, name)
            self.addCleanup(signal.signal, number, signal.getsignal(number))

    def test_termination_signals_are_handled(self):
        daemon = self.daemon()
        daemon.install_signal_handlers()
        for name in ("SIGTERM", "SIGINT"):
            with self.subTest(signal=name):
                self.assertEqual(
                    signal.getsignal(getattr(signal, name)),
                    daemon._handle_shutdown_signal,
                )

    def test_sighup_is_handled_separately(self):
        # Conventionally "reload config", not "stop"; wiring it to the shutdown
        # handler would turn every systemctl reload into an outage.
        daemon = self.daemon()
        daemon.install_signal_handlers()
        self.assertEqual(signal.getsignal(signal.SIGHUP), daemon._handle_reload_signal)

    def test_a_delivered_shutdown_signal_only_sets_flags(self):
        daemon = self.daemon()
        daemon.install_signal_handlers()
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(daemon._shutdown_requested)
        self.assertEqual(daemon._shutdown_signal, signal.SIGTERM)

    def test_a_delivered_hangup_only_sets_a_flag(self):
        daemon = self.daemon()
        daemon.install_signal_handlers()
        os.kill(os.getpid(), signal.SIGHUP)
        self.assertTrue(daemon._reload_requested)
        self.assertFalse(daemon._shutdown_requested)


class SdNotifierTests(unittest.TestCase):
    """Without NOTIFY_SOCKET everything is a no-op, which is what makes the
    daemon runnable outside systemd at all."""

    def build_without_systemd(self) -> SdNotifier:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOTIFY_SOCKET", None)
            return SdNotifier()

    def test_no_socket_is_opened_without_notify_socket(self):
        notifier = self.build_without_systemd()
        with patch("lib.daemon.socket.socket") as sock:
            notifier.ready()
            notifier.stopping()
            notifier.watchdog()
            notifier.status("running")
        sock.assert_not_called()

    def test_the_address_is_read_once_at_construction(self):
        # So a test, or a reload, cannot change where an existing notifier
        # points halfway through the daemon's life.
        notifier = self.build_without_systemd()
        with patch.dict(os.environ, {"NOTIFY_SOCKET": "/run/systemd/notify"}):
            self.assertIsNone(notifier._addr)


@_POSIX_ONLY
class SdNotifierSocketTests(unittest.TestCase):
    """Driven against a real datagram socket, the way systemd listens."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        self.path = str(Path(self.tmpdir) / "notify.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.sock.settimeout(2.0)
        self.addCleanup(self.sock.close)
        env = patch.dict(os.environ, {"NOTIFY_SOCKET": self.path}, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def received(self) -> bytes:
        return self.sock.recv(4096)

    def test_ready_is_sent(self):
        SdNotifier().ready()
        self.assertEqual(self.received(), b"READY=1")

    def test_stopping_is_sent(self):
        SdNotifier().stopping()
        self.assertEqual(self.received(), b"STOPPING=1")

    def test_a_watchdog_ping_is_sent(self):
        SdNotifier().watchdog()
        self.assertEqual(self.received(), b"WATCHDOG=1")

    def test_a_status_line_is_sent(self):
        SdNotifier().status("running")
        self.assertEqual(self.received(), b"STATUS=running")

    def test_an_abstract_socket_address_is_rewritten(self):
        # systemd hands over "@name" for an abstract-namespace socket, which
        # the kernel expects as a leading NUL byte.
        name = f"fand-test-{os.getpid()}"
        abstract = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        abstract.bind("\0" + name)
        abstract.settimeout(2.0)
        self.addCleanup(abstract.close)
        with patch.dict(os.environ, {"NOTIFY_SOCKET": "@" + name}):
            SdNotifier().ready()
        self.assertEqual(abstract.recv(4096), b"READY=1")

    def test_an_unreachable_socket_is_swallowed(self):
        # systemd absent, or the socket already torn down during shutdown,
        # must never raise into the daemon.
        with patch.dict(os.environ, {"NOTIFY_SOCKET": self.path + ".absent"}):
            notifier = SdNotifier()
        notifier.ready()

    def test_a_failure_is_logged_at_debug_only(self):
        # It is not an error: most of the time there is simply no systemd.
        with patch.dict(os.environ, {"NOTIFY_SOCKET": self.path + ".absent"}):
            notifier = SdNotifier()
        with self.assertLogs("lib.daemon", level="DEBUG") as logs:
            notifier.ready()
        self.assertTrue([line for line in logs.output if "sd_notify failed" in line])


if __name__ == "__main__":
    unittest.main()
