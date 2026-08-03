"""Centralized logging configuration."""

from __future__ import annotations

import logging
import sys

_FORMAT = "%(name)s: %(levelname)s: %(message)s"


def configure(verbose: bool = False, *, stream=sys.stdout) -> None:
    """Configure root logging with journald-friendly formatting.

    No timestamp/PID prefix: journald adds its own metadata.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format=_FORMAT, stream=stream)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def set_level(level_name: str) -> None:
    """Adjust the root logger's level after initial configure(), e.g. once
    config.toml's daemon.log_level is known.
    """
    level = logging.getLevelName(level_name.upper())
    if not isinstance(level, int):
        raise ValueError(f"invalid log level: {level_name!r}")
    logging.getLogger().setLevel(level)
