"""Loads daemon and VM configuration from a config directory."""

from __future__ import annotations

import tomllib
from pathlib import Path

from lib.models.config import Config
from lib.models.vm import VMConfig


class ConfigError(Exception):
    """Raised when a config file is missing, malformed, or incomplete."""


class ConfigManager:
    """Loads config.toml and vms/*.toml from a config directory."""

    def __init__(self, config_dir: Path) -> None:
        self._config_dir = Path(config_dir)
        self.config: Config | None = None
        self.vms: dict[str, VMConfig] = {}

    def load(self) -> None:
        self.config = self._load_config()
        self.vms = self._discover_vms()

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
