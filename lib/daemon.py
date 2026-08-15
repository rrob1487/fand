#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import signal
import socket
import time
from pathlib import Path

from lib.controller import Controller
from lib.hardware.ipmi import IPMI, IPMIFanController
from lib.managers.config_manager import ConfigError, ConfigManager
from lib.managers.notification_manager import NotificationManager
from lib.managers.sensor_manager import SensorManager
from lib.managers.vm_manager import VMManager
from lib.policy import Policy
from lib.utils.logging import set_level


# --------------------------------------------------------------------------
# sd_notify: minimal stdlib implementation (no external dependency)
# --------------------------------------------------------------------------
class SdNotifier:
    """Talks to systemd's NOTIFY_SOCKET, if present. No-op otherwise."""

    def __init__(self) -> None:
        self._addr = os.environ.get("NOTIFY_SOCKET")

    def _send(self, message: str) -> None:
        if not self._addr:
            return
        addr = self._addr
        if addr.startswith("@"):
            addr = "\0" + addr[1:]  # abstract namespace socket
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.connect(addr)
                sock.sendall(message.encode())
        except OSError as exc:
            logging.getLogger(__name__).debug("sd_notify failed: %s", exc)

    def ready(self) -> None:
        self._send("READY=1")

    def stopping(self) -> None:
        self._send("STOPPING=1")

    def watchdog(self) -> None:
        self._send("WATCHDOG=1")

    def status(self, text: str) -> None:
        self._send(f"STATUS={text}")


# --------------------------------------------------------------------------
# Daemon
# --------------------------------------------------------------------------
class Daemon:
    def __init__(
        self,
        config_dir: Path,
        poll_interval: float | None = None,
        dry_run: bool = False,
        verbose: bool = False,
    ) -> None:
        self.config_dir = config_dir
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self.verbose = verbose
        self.log = logging.getLogger("fand")
        self.notifier = SdNotifier()
        self._shutdown_requested = False
        self._shutdown_signal: int | None = None
        self._reload_requested = False

        self._config_manager: ConfigManager | None = None
        self._ipmi: IPMI | None = None
        self._controller: Controller | None = None
        self._notification_manager: NotificationManager | None = None

        # Watchdog interval, if the unit sets WatchdogSec=. systemd exposes
        # it as WATCHDOG_USEC (microseconds).
        watchdog_usec = os.environ.get("WATCHDOG_USEC")
        self._watchdog_interval = (
            int(watchdog_usec) / 1_000_000 / 2 if watchdog_usec else None
        )
        self._last_watchdog_ping = 0.0

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)
        # SIGHUP is conventionally "reload config", not "stop".
        signal.signal(signal.SIGHUP, self._handle_reload_signal)

    # Both handlers only set a flag. A signal handler runs on the main thread
    # between bytecodes and can interrupt anything -- including a logging call
    # that already holds the module lock, where logging again deadlocks. That
    # was survivable while the daemon was single-threaded; now that notifier
    # workers log on every delivery, retry, and drop, the lock is held a large
    # fraction of the time. The work happens in run(), where the flags are read.
    def _handle_shutdown_signal(self, signum: int, _frame) -> None:
        self._shutdown_signal = signum
        self._shutdown_requested = True

    def _handle_reload_signal(self, _signum: int, _frame) -> None:
        self._reload_requested = True

    def reload_config(self) -> None:
        # A bad edit here shouldn't take down an otherwise-healthy running
        # daemon, so failures are logged and the previous good config/
        # Controller stay in effect (unlike setup(), where failure is fatal).
        try:
            self._config_manager.reload()
            self._apply_log_level()
            self._controller = self._build_controller()
        except ConfigError as exc:
            self.log.error("reload failed, keeping previous configuration: %s", exc)
            return
        self.log.info("Configuration reloaded")

        # Separately guarded: a notification problem must never undo a
        # fan-control configuration reload that already succeeded.
        if self._notification_manager is not None:
            try:
                self._notification_manager.reload(self._config_manager.notifiers)
            except Exception:
                self.log.exception("notifier reload failed, keeping previous notifiers")

    def setup(self) -> None:
        """One-time startup work: load config, discover sensors, build the
        control pipeline."""
        self.log.info("Starting up")
        self._config_manager = ConfigManager(self.config_dir)
        self._config_manager.load()
        self._apply_log_level()

        if self.poll_interval is None:
            self.poll_interval = self._config_manager.config.daemon.poll_interval

        self._notification_manager = self._build_notification_manager()
        if self._notification_manager is not None:
            self._notification_manager.start()

        self._ipmi = IPMI()
        self._controller = self._build_controller()

    def _build_notification_manager(self) -> NotificationManager | None:
        """Create the notification manager, or run without notifications.

        Created here rather than in _build_controller() because that runs again
        on every reload: a manager built there would respawn every worker
        thread and discard every queued job on each SIGHUP, including reloads
        that changed nothing about notifications.

        Guarded because notification is non-critical. A bug here must not stop
        the daemon cooling the machine.
        """
        try:
            return NotificationManager(
                self._config_manager.notifiers, dry_run=self.dry_run,
            )
        except Exception:
            self.log.exception("notifications disabled: could not be initialised")
            return None

    def _apply_log_level(self) -> None:
        # -v/--verbose is an explicit CLI override and always wins; only
        # fall back to config.toml's daemon.log_level when it wasn't passed.
        if self.verbose:
            return
        try:
            set_level(self._config_manager.config.daemon.log_level)
        except ValueError as exc:
            self.log.warning("invalid config log_level, keeping current level: %s", exc)

    def _build_controller(self) -> Controller:
        vm_manager = VMManager(self._config_manager.vms)
        sensor_manager = SensorManager(
            vm_manager,
            self._ipmi,
            rediscover_interval=self._config_manager.config.daemon.sensor_rediscover_interval,
        )
        sensor_manager.discover()
        policy = Policy(self._config_manager.config.fan_curve, self._config_manager.config.safety)
        fan_controller = IPMIFanController(self._ipmi)
        return Controller(
            sensor_manager, policy, fan_controller, dry_run=self.dry_run,
            notification_manager=self._notification_manager,
        )

    def do_work(self) -> None:
        """One iteration of the daemon's actual job: run one control cycle."""
        self._controller.run_cycle()

    def maybe_ping_watchdog(self) -> None:
        if self._watchdog_interval is None:
            return
        now = time.monotonic()
        if now - self._last_watchdog_ping >= self._watchdog_interval:
            self.notifier.watchdog()
            self._last_watchdog_ping = now

    def run(self) -> int:
        self.install_signal_handlers()
        self.setup()

        # Tell systemd we're up. Only meaningful for Type=notify units;
        # harmless no-op otherwise.
        self.notifier.ready()
        self._last_watchdog_ping = time.monotonic()
        self.notifier.status("running")
        self.log.info("Daemon is ready, entering main loop")

        exit_code = 0
        try:
            while not self._shutdown_requested:
                if self._reload_requested:
                    # Deferred out of the SIGHUP handler; see the note there.
                    self._reload_requested = False
                    self.log.info("Received SIGHUP, reloading configuration")
                    self.reload_config()

                try:
                    self.do_work()
                except Exception:
                    # Don't let one bad iteration kill the whole daemon.
                    # If a failure should be fatal, re-raise or set exit_code
                    # and break instead.
                    self.log.exception("Critical hardware failure")

                self.maybe_ping_watchdog()
                time.sleep(self.poll_interval)
        except Exception:
            self.log.exception("Fatal error, shutting down")
            exit_code = 1
        finally:
            if self._shutdown_signal is not None:
                self.log.info(
                    "Received signal %s, shutting down gracefully",
                    self._shutdown_signal,
                )
            self.notifier.stopping()
            self.teardown()

        return exit_code

    def run_notify_test(self) -> int:
        """Deliver one synthetic notification per notifier and report.

        Validates configuration, credentials, and reachability without running
        the daemon: no IPMI, no sensors, no control loop, no worker threads.
        Every notifier is attempted so one bad credential does not hide the
        state of the rest.
        """
        self._config_manager = ConfigManager(self.config_dir)
        self._config_manager.load()
        self._apply_log_level()

        manager = NotificationManager(
            self._config_manager.notifiers, dry_run=self.dry_run,
        )
        enabled = {
            key for key, config in self._config_manager.notifiers.items()
            if config.enabled
        }
        if not enabled:
            self.log.warning("no enabled notifiers are configured in %s", self.config_dir)
            return 0

        results = manager.self_test()
        for result in results:
            if result.ok:
                self.log.info("notifier %r: %s", result.notifier, result.detail)
            else:
                self.log.error("notifier %r: FAILED: %s", result.notifier, result.detail)

        # A notifier that could not even be built counts as a failure here.
        # The manager already logged why; reporting it as a pass because it
        # never ran would defeat the point of asking for a test.
        unbuilt = sorted(enabled - {r.notifier for r in results})
        for key in unbuilt:
            self.log.error("notifier %r: FAILED: could not be created", key)

        failed = len(unbuilt) + sum(1 for r in results if not r.ok)
        if failed:
            self.log.error("%d of %d notifiers failed", failed, len(enabled))
            return 1
        self.log.info("all %d notifiers delivered successfully", len(results))
        return 0

    def teardown(self) -> None:
        """One-time cleanup work: release fan control back to iDRAC.

        Ordering is a safety requirement, not a preference. Fan control is
        released first, in its own guard, so notification shutdown is
        structurally incapable of delaying or preventing it -- leaving the fans
        pinned under manual control with no daemon driving them is the failure
        this ordering exists to make impossible.
        """
        try:
            if self._controller is not None:
                self._controller.release_fan_control()
        except Exception:
            self.log.exception("failed to release fan control")
        finally:
            if self._notification_manager is not None:
                try:
                    self._notification_manager.stop()
                except Exception:
                    self.log.exception("notification shutdown failed")
        self.log.info("Shut down cleanly")

