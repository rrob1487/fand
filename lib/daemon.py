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

        self._config_manager: ConfigManager | None = None
        self._ipmi: IPMI | None = None
        self._controller: Controller | None = None

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

    def _handle_shutdown_signal(self, signum: int, _frame) -> None:
        self.log.info("Received signal %s, shutting down gracefully", signum)
        self._shutdown_requested = True

    def _handle_reload_signal(self, _signum: int, _frame) -> None:
        self.log.info("Received SIGHUP, reloading configuration")
        self.reload_config()

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
        else:
            self.log.info("Configuration reloaded")

    def setup(self) -> None:
        """One-time startup work: load config, discover sensors, build the
        control pipeline."""
        self.log.info("Starting up")
        self._config_manager = ConfigManager(self.config_dir)
        self._config_manager.load()
        self._apply_log_level()

        if self.poll_interval is None:
            self.poll_interval = self._config_manager.config.daemon.poll_interval

        self._ipmi = IPMI()
        self._controller = self._build_controller()

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
        sensor_manager = SensorManager(vm_manager, self._ipmi)
        sensor_manager.discover()
        policy = Policy(self._config_manager.config.fan_curve, self._config_manager.config.safety)
        fan_controller = IPMIFanController(self._ipmi)
        return Controller(sensor_manager, policy, fan_controller, dry_run=self.dry_run)

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
            self.notifier.stopping()
            self.teardown()

        return exit_code

    def teardown(self) -> None:
        """One-time cleanup work: close connections, flush buffers, etc."""
        self.log.info("Shut down cleanly")
        # TODO: real cleanup goes here.

