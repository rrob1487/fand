"""Dell IPMI interface: temperature sensors, raw commands, fan control.

Kept as one module (per docs/build_order.md's "IPMI Dual Role" note) so
Dell-specific raw-command knowledge stays in one place rather than being
duplicated across sensor and fan-controller implementations.
"""

from __future__ import annotations

import re
import subprocess

from lib.hardware.sensor import Sensor
from lib.utils.logging import get_logger

_log = get_logger(__name__)

# Confirmed against real PowerEdge/iDRAC hardware.
_RAW_MODE_MANUAL = ("0x30", "0x30", "0x01", "0x00")
_RAW_MODE_AUTOMATIC = ("0x30", "0x30", "0x01", "0x01")
_RAW_SET_SPEED_PREFIX = ("0x30", "0x30", "0x02", "0xff")

# "19 degrees C", "-12 degrees C", "23.5 Degrees C". A row with no leading
# number ("Disabled", "No Reading", "ns") simply does not match.
_READING = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(.*)$")

# Warned about once per process rather than once per sensor per poll.
_warned_fahrenheit = False


class IPMIError(Exception):
    """Raised when an ipmitool invocation fails or returns unparsable output."""


def _sort_key(sensor_id: str) -> tuple[int, object]:
    """Order sensor IDs numerically ("0Eh" before "0Fh"), by text if unparsable.

    Sorting "10h" after "0Fh" is the whole reason this is not a string sort.
    An ID the BMC formats unexpectedly falls back to its text and sorts after
    every parsable one, so it still lands somewhere deterministic.
    """
    try:
        return (0, int(sensor_id.rstrip("hH"), 16))
    except ValueError:
        return (1, sensor_id)


def _parse_celsius(reading: str) -> float | None:
    """A reading in degrees Celsius, or None if there isn't one.

    Detected by failing to parse a leading number rather than by matching
    wording: iDRAC spells an absent reading "Disabled", "No Reading" or "ns"
    depending on version, and a healthy BMC never shows which one it uses.

    Fahrenheit is converted rather than trusted. iDRAC can be configured to
    report it, every threshold in config.toml is Celsius, and an unconverted
    104F would read as 104C and trip an emergency shutdown on an idle machine.
    """
    global _warned_fahrenheit

    match = _READING.match(reading.strip())
    if match is None:
        return None
    value, unit = float(match.group(1)), match.group(2).strip().lower()
    if unit in ("degrees c", "degree c", "c"):
        return value
    if unit in ("degrees f", "degree f", "f"):
        if not _warned_fahrenheit:
            _warned_fahrenheit = True
            _log.warning(
                "BMC is reporting temperatures in Fahrenheit; converting to "
                "Celsius, since every configured threshold is Celsius",
            )
        return (value - 32.0) * 5.0 / 9.0
    # A number in units we cannot name is worse than no reading: the caller
    # would compare it against a Celsius threshold.
    return None


class IPMI:
    """Runs ipmitool commands against the local BMC."""

    def __init__(self, ipmitool_path: str = "/usr/bin/ipmitool", timeout: float = 5.0) -> None:
        self._ipmitool_path = ipmitool_path
        self._timeout = timeout
        # This cycle's SDR temperature table, or None when it needs re-reading.
        self._temperature_cache: dict[str, float | None] | None = None

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

    # ------------------------------------------------------------------
    # Temperature sensors
    # ------------------------------------------------------------------
    def begin_cycle(self) -> None:
        """Drop the cached SDR table so the next read re-queries the BMC.

        Called once per control cycle. Without it every IPMISensor triggers its
        own full table walk, multiplying BMC latency by the sensor count inside
        the same cycle that has to kick the watchdog afterwards.

        Invalidation is explicit rather than time-based on purpose: a cycle's
        readings are then a function of its inputs alone, not of how long the
        cycle happened to take.
        """
        self._temperature_cache = None

    def temperature_readings(self) -> dict[str, float | None]:
        """Every temperature sensor the SDR lists, in degrees Celsius.

        A value of None means the SDR knows the sensor but has no reading for
        it right now. That is a failed sensor, never an absent one -- the
        distinction is the difference between a sensor that recovers by itself
        and one that silently disappears until the daemon is restarted.

        Cached for the rest of the cycle, so every sensor in a cycle reads the
        same sample. Only successful parses are cached: a failed invocation
        must reach the BMC again when the caller retries.
        """
        if self._temperature_cache is None:
            self._temperature_cache = self._parse_sdr_temperature_table(
                self._run("sdr", "type", "temperature")
            )
        return self._temperature_cache

    def temperature_sensor_names(self) -> list[str]:
        """Names of every temperature sensor the BMC reports, readable or not.

        Sensor count and naming vary by chassis (dual-CPU PowerEdge boards
        report two sensors both called "Temp"), so the set is discovered from
        hardware rather than hand-enumerated in configuration.
        """
        return list(self.temperature_readings())

    @staticmethod
    def _parse_sdr_temperature_table(output: str) -> dict[str, float | None]:
        """Parse `ipmitool sdr type temperature` into {name: celsius | None}.

        Rows carry five fields -- name, sensor ID, status, entity ID, reading:

            Inlet Temp       | 04h | ok  |  7.1 | 19 degrees C
            Temp             | 0Eh | ns  |  3.1 | Disabled

        Repeated names are disambiguated by sensor ID, not by position in the
        output: "Temp" is 0Eh and "Temp #2" is 0Fh whichever order the BMC
        walks its SDR in and whichever of them can be read right now. A suffix
        assigned in encounter order silently re-points at the other CPU as soon
        as one of them stops reporting, which would make the same hardware
        produce different decisions.
        """
        rows: list[tuple[str, str, str]] = []
        for line in output.splitlines():
            fields = [field.strip() for field in line.split("|")]
            if len(fields) < 5:
                continue  # banner lines and truncated rows
            name, sensor_id, reading = fields[0], fields[1], fields[4]
            if not name:
                continue
            rows.append((name, sensor_id, reading))

        # Rank each row among its same-named siblings by sensor ID, then emit
        # in the BMC's own order so the caller still sees the chassis layout.
        ranks: dict[int, int] = {}
        for name in {row[0] for row in rows}:
            indexed = [(i, rows[i][1]) for i in range(len(rows)) if rows[i][0] == name]
            for rank, (index, _id) in enumerate(sorted(indexed, key=lambda p: _sort_key(p[1])), 1):
                ranks[index] = rank

        readings: dict[str, float | None] = {}
        for index, (name, _sensor_id, reading) in enumerate(rows):
            rank = ranks[index]
            key = name if rank == 1 else f"{name} #{rank}"
            readings[key] = _parse_celsius(reading)
        return readings


class IPMISensor(Sensor):
    """Reads a single named sensor from the BMC's SDR temperature table."""

    def __init__(self, ipmi: IPMI, sensor_name: str = "Exhaust Temp") -> None:
        self._ipmi = ipmi
        self._sensor_name = sensor_name

    def read(self) -> float:
        """The sensor's temperature, or IPMIError if it has none right now.

        Raising covers both "the SDR no longer lists this sensor" and "the SDR
        lists it but has no reading". Both are failures rather than readings:
        substituting a number the BMC did not give us -- 0.0, say -- would read
        as ice-cold and idle the fans on a hot chassis. Raising instead routes
        the sensor into SensorManager's failed-sensor handling, which warns once
        and lets it recover by itself.
        """
        readings = self._ipmi.temperature_readings()
        if self._sensor_name not in readings:
            raise IPMIError(
                f"sensor {self._sensor_name!r} not found in ipmitool sdr output"
            )
        value = readings[self._sensor_name]
        if value is None:
            raise IPMIError(f"sensor {self._sensor_name!r} has no reading")
        return value


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
