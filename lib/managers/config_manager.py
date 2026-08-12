"""Loads daemon, VM, and notifier configuration from a config directory.

Core configuration fails loudly: a missing fan curve means the machine cannot
be cooled, so startup must stop. Notifier configuration fails leniently: a bad
file means a message does not get sent, so it is logged and skipped while every
other notifier keeps working.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from lib.models.config import Config
from lib.models.notification import NotifierConfig, NotifierConfigError
from lib.models.vm import VMConfig
from lib.utils.logging import get_logger

_log = get_logger(__name__)


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or incomplete."""


class ConfigManager:
    """Loads config.toml, vms/*.toml, and notification/*.toml."""

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = Path(config_dir)
        self.config: Config | None = None
        self.vms: dict[str, VMConfig] = {}
        self.notifiers: dict[str, NotifierConfig] = {}

    def load(self) -> None:
        """Read every configuration file, or change nothing.

        Everything is built locally and assigned at the end so a failure
        leaves the previous configuration intact. Assigning as each piece
        loaded would let a failed reload advance one part while stranding
        another, and Daemon.reload_config's "keeping previous configuration"
        would no longer be true.
        """
        config = self._load_config()
        vms = self._discover_vms()
        notifiers = self._discover_notifiers(config)

        self.config = config
        self.vms = vms
        self.notifiers = notifiers

    def reload(self) -> None:
        self.load()

    def _load_toml(self, path: Path) -> dict:
        try:
            with open(path, "rb") as f:
                return tomllib.load(f)
        except FileNotFoundError as exc:
            raise ConfigError(f"config file not found: {path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    def _load_config(self) -> Config:
        path = self._config_dir / "config.toml"
        data = self._load_toml(path)
        try:
            return Config.from_dict(data)
        except KeyError as exc:
            raise ConfigError(f"missing required key {exc} in {path}") from exc

    def _discover_vms(self) -> dict[str, VMConfig]:
        vms_dir = self._config_dir / "vms"
        vms: dict[str, VMConfig] = {}
        if not vms_dir.is_dir():
            return vms
        for path in sorted(vms_dir.glob("*.toml")):
            data = self._load_toml(path)
            try:
                vm = VMConfig.from_dict(data)
            except KeyError as exc:
                raise ConfigError(f"missing required key {exc} in {path}") from exc
            vms[vm.name] = vm
        return vms

    def _discover_notifiers(self, config: Config) -> dict[str, NotifierConfig]:
        """Load notification/*.toml, skipping any file that cannot be used.

        Keyed by file stem rather than by Name: notification.md states Name
        need not be unique, so the file is the only identity that cannot
        collide -- which is what the notification manager matches on to decide
        which notifiers survive a reload untouched.

        Unlike vms/*.toml, a bad file here is a warning rather than a fatal
        error. Raising would stop the daemon starting and take the valid
        notifiers down with it, which notification.md explicitly forbids.
        """
        notification_dir = self._config_dir / "notification"
        notifiers: dict[str, NotifierConfig] = {}
        if not notification_dir.is_dir():
            return notifiers

        for path in sorted(notification_dir.glob("*.toml")):
            try:
                notifier = NotifierConfig.from_dict(self._load_toml(path))
            except (ConfigError, NotifierConfigError, OSError) as exc:
                # OSError included so one unreadable file cannot stop startup.
                _log.warning("ignoring notifier config %s: %s", path.name, exc)
                continue
            self._warn_if_interval_unreachable(config, notifier, path)
            notifiers[path.stem] = notifier
        return notifiers

    @staticmethod
    def _warn_if_interval_unreachable(
        config: Config, notifier: NotifierConfig, path: Path,
    ) -> None:
        """Notifiers are evaluated on the control loop, so one cannot fire
        more often than the daemon polls. The notifier still loads; it just
        runs at the poll cadence instead of the one its file asks for."""
        poll_interval = config.daemon.poll_interval
        if notifier.interval_seconds < poll_interval:
            _log.warning(
                "notifier config %s asks for Interval=%.3gs but the daemon polls "
                "every %.3gs; it will notify no more often than that",
                path.name, notifier.interval_seconds, poll_interval,
            )
