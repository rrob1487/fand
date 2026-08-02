"""Dell IPMI interface: temperature sensors, raw commands, fan control.

Kept as one module (per docs/build_order.md's "IPMI Dual Role" note) so
Dell-specific raw-command knowledge stays in one place rather than being
duplicated across sensor and fan-controller implementations.
"""

from __future__ import annotations

import subprocess

from lib.hardware.sensor import Sensor

# Confirmed against real PowerEdge/iDRAC hardware.
_RAW_MODE_MANUAL = ("0x30", "0x30", "0x01", "0x00")
_RAW_MODE_AUTOMATIC = ("0x30", "0x30", "0x01", "0x01")
_RAW_SET_SPEED_PREFIX = ("0x30", "0x30", "0x02", "0xff")


class IPMIError(Exception):
    """Raised when an ipmitool invocation fails or returns unparsable output."""


class IPMI:
    """Runs ipmitool commands against the local BMC."""

    def __init__(self, ipmitool_path: str = "/usr/bin/ipmitool", timeout: float = 5.0) -> None:
        self._ipmitool_path = ipmitool_path
        self._timeout = timeout

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(
                [self._ipmitool_path, *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise IPMIError(f"ipmitool invocation failed: {exc}") from exc

        if result.returncode != 0:
            raise IPMIError(
                f"ipmitool {' '.join(args)} exited {result.returncode}: {result.stderr.strip()}"
            )
        return result.stdout

    def raw_command(self, *bytes_: str) -> str:
        """Send a raw IPMI command. bytes_ are hex strings like '0x30'."""
        return self._run("raw", *bytes_)

    def sensor_readings(self) -> dict[str, float]:
        """Parse `ipmitool sensor` into {name: reading} for numeric sensors.

        Sensors sharing a name (e.g. dual-CPU "Temp" on PowerEdge boards)
        are disambiguated in encounter order: "Temp", "Temp #2", ...
        """
        table = self._parse_sensor_table(self._run("sensor"))
        return {name: value for name, (value, _unit) in table.items()}

    def temperature_sensor_names(self) -> list[str]:
        """Names of every sensor reporting a temperature unit.

        Excludes non-temperature numeric sensors (fan RPM, voltage,
        power draw, ...) so callers building temperature Sensor objects
        don't accidentally wire one up to a voltage rail.
        """
        table = self._parse_sensor_table(self._run("sensor"))
        return [name for name, (_value, unit) in table.items() if "degree" in unit.lower()]

    @staticmethod
    def _parse_sensor_table(output: str) -> dict[str, tuple[float, str]]:
        readings: dict[str, tuple[float, str]] = {}
        seen: dict[str, int] = {}
        for line in output.splitlines():
            if "|" not in line:
                continue
            fields = [field.strip() for field in line.split("|")]
            if len(fields) < 3:
                continue
            name, value, unit = fields[0], fields[1], fields[2]
            if not name or not value:
                continue
            try:
                reading = float(value)
            except ValueError:
                continue  # "na" / discrete sensors have no numeric reading
            seen[name] = seen.get(name, 0) + 1
            key = name if seen[name] == 1 else f"{name} #{seen[name]}"
            readings[key] = (reading, unit)
        return readings


class IPMISensor(Sensor):
    """Reads a single named sensor from `ipmitool sensor`'s output table."""

    def __init__(self, ipmi: IPMI, sensor_name: str = "Exhaust Temp") -> None:
        self._ipmi = ipmi
        self._sensor_name = sensor_name

    def read(self) -> float:
        readings = self._ipmi.sensor_readings()
        try:
            return readings[self._sensor_name]
        except KeyError as exc:
            raise IPMIError(
                f"sensor {self._sensor_name!r} not found in ipmitool sensor output"
            ) from exc


class IPMIFanController:
    """Dell-specific manual fan speed control via documented iDRAC raw commands."""

    def __init__(self, ipmi: IPMI) -> None:
        self._ipmi = ipmi

    def enable_manual_control(self) -> None:
        self._ipmi.raw_command(*_RAW_MODE_MANUAL)

    def enable_automatic_control(self) -> None:
        self._ipmi.raw_command(*_RAW_MODE_AUTOMATIC)

    def set_speed(self, percent: float) -> None:
        """Set all fans to the given percent (0-100). Enables manual control first."""
        percent = max(0, min(100, percent))
        self.enable_manual_control()
        self._ipmi.raw_command(*_RAW_SET_SPEED_PREFIX, f"0x{int(percent):02x}")
