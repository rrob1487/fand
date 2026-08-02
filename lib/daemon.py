#!/usr/bin/env python3

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path


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
    def __init__(self, poll_interval: float = 5.0, dry_run: bool = False) -> None:
        self.poll_interval = poll_interval
        self.dry_run = dry_run
        self.log = logging.getLogger("fand")
        self.notifier = SdNotifier()
        self._shutdown_requested = False

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
        # TODO: re-read config file(s) here.
        pass

    def setup(self) -> None:
        """One-time startup work: open connections, warm caches, etc."""
        self.log.info("Starting up")
        # TODO: real initialization goes here.

    def do_work(self) -> None:
        """One iteration of the daemon's actual job. Replace this."""
        self.log.debug("Doing work-" + str(time.monotonic()))

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


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        # No timestamp/PID prefix needed: journald adds its own metadata.
        format="%(name)s: %(levelname)s: %(message)s",
        stream=sys.stdout,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="mydaemon")
    parser.add_argument(
        "-c", "--config", type=Path, default=Path("/opt/fand/config"),
        help="Path to config directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not change hardware state"
    )
    parser.add_argument(
        "--poll-interval", type=float, default=10.0,
        help="Seconds between work iterations",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    configure_logging(args.verbose)

    daemon = Daemon(poll_interval=args.poll_interval, dry_run=args.dry_run)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())

