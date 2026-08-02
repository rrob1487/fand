#!/usr/bin/env python3
"""Application entry point for fand.

Responsible for CLI argument parsing, logging initialization, and
environment loading. Application logic belongs in the daemon and its
lower layers, not here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from lib.daemon import Daemon
from lib.utils.logging import configure as configure_logging


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from an env file into os.environ.

    Values already present in the environment take precedence, so real
    environment variables can always override the file.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fand: fan-control daemon")
    parser.add_argument(
        "-c", "--config", type=Path, default=Path("/opt/fand/config"),
        help="Path to config directory",
    )
    parser.add_argument(
        "--env-file", type=Path, default=Path("/opt/fand/.env"),
        help="Path to environment file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not change hardware state",
    )
    parser.add_argument(
        "--poll-interval", type=float, default=None,
        help="Seconds between work iterations (overrides config.toml's daemon.poll_interval)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    load_env_file(args.env_file)
    configure_logging(args.verbose)

    daemon = Daemon(config_dir=args.config, poll_interval=args.poll_interval, dry_run=args.dry_run)
    return daemon.run()


if __name__ == "__main__":
    sys.exit(main())