"""Tests for lib/hardware/ipmi.py.

No ipmitool is ever invoked: subprocess.run is replaced by a recording fake, so
the assertions are about the exact argv and the exact raw bytes we hand the BMC.
What those bytes then do to a real iDRAC is not something a test can prove --
that is verified on the R730 itself.
"""

from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from lib.hardware.ipmi import (
    IPMI,
    IPMIError,
    IPMIFanController,
    IPMISensor,
)

# A trimmed but otherwise faithful `ipmitool sensor` table from a PowerEdge:
# two identically-named CPU sensors, non-temperature numeric sensors, an
# unreadable sensor, and a discrete one.
_SENSOR_TABLE = """\
Temp             | 41.000     | degrees C  | ok    | na        | 3.000     | 8.000     | 82.000    | 87.000    | na
Temp             | 40.000     | degrees C  | ok    | na        | 3.000     | 8.000     | 82.000    | 87.000    | na
Inlet Temp       | 22.000     | degrees C  | ok    | na        | -7.000    | 3.000     | 42.000    | 47.000    | na
Exhaust Temp     | 34.000     | degrees C  | ok    | na        | 3.000     | 8.000     | 70.000    | 75.000    | na
Fan1A            | 6000.000   | RPM        | ok    | na        | 600.000   | 840.000   | na        | na        | na
Fan2A            | 5040.000   | RPM        | ok    | na        | 600.000   | 840.000   | na        | na        | na
Voltage 1        | na         |            | na    | na        | na        | na        | na        | na        | na
Current 1        | 0.200      | Amps       | ok    | na        | na        | na        | na        | na        | na
Pwr Consumption  | 154.000    | Watts      | ok    | na        | na        | na        | na        | 896.000   | 980.000
Status           | 0x00       | discrete   | 0x0080| na        | na        | na        | na        | na        | na
"""


class FakeRun:
    """Stands in for subprocess.run, recording every invocation."""

    def __init__(self, stdout="", stderr="", returncode=0, error=None):
        self.calls: list[tuple[list[str], dict]] = []
        self._stdout = stdout
        self._stderr = stderr
        self._returncode = returncode
        self._error = error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self._error is not None:
            raise self._error
        return subprocess.CompletedProcess(
            argv, self._returncode, self._stdout, self._stderr,
        )

    @property
    def argv(self) -> list[str]:
        return self.calls[-1][0]


class FakeIPMI:
    """Records raw commands instead of sending them."""

    def __init__(self, error=None):
        self.commands: list[tuple[str, ...]] = []
        self._error = error

    def raw_command(self, *bytes_: str) -> str:
        self.commands.append(bytes_)
        if self._error is not None:
            raise self._error
        return ""


class IPMITestCase(unittest.TestCase):
    def run_with(self, **kwargs) -> FakeRun:
        fake = FakeRun(**kwargs)
        patcher = patch("lib.hardware.ipmi.subprocess.run", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake


# ---------------------------------------------------------------------------
# Invocation
# ---------------------------------------------------------------------------
class InvocationTests(IPMITestCase):
    def test_the_configured_binary_is_used(self):
        fake = self.run_with(stdout="ok")
        IPMI(ipmitool_path="/opt/bin/ipmitool").raw_command("0x30")
        self.assertEqual(fake.argv[0], "/opt/bin/ipmitool")

    def test_the_default_binary_is_the_usual_path(self):
        fake = self.run_with(stdout="ok")
        IPMI().raw_command("0x30")
        self.assertEqual(fake.argv[0], "/usr/bin/ipmitool")

    def test_raw_commands_are_prefixed_with_raw(self):
        fake = self.run_with(stdout="ok")
        IPMI().raw_command("0x30", "0x30", "0x01", "0x00")
        self.assertEqual(fake.argv[1:], ["raw", "0x30", "0x30", "0x01", "0x00"])

    def test_the_configured_timeout_is_passed_through(self):
        fake = self.run_with(stdout="ok")
        IPMI(timeout=2.5).raw_command("0x30")
        self.assertEqual(fake.calls[-1][1]["timeout"], 2.5)

    def test_output_is_captured_as_text_without_raising(self):
        # check=False, because a non-zero exit is turned into IPMIError with
        # the stderr attached rather than an opaque CalledProcessError.
        fake = self.run_with(stdout="ok")
        IPMI().raw_command("0x30")
        kwargs = fake.calls[-1][1]
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])
        self.assertFalse(kwargs["check"])

    def test_stdout_is_returned(self):
        self.run_with(stdout="  01 02 03\n")
        self.assertEqual(IPMI().raw_command("0x30"), "  01 02 03\n")


class InvocationFailureTests(IPMITestCase):
    """Every failure mode has to arrive as IPMIError, because that is the only
    exception the Controller's retry and the fan-speed error path look for."""

    def test_a_non_zero_exit_raises(self):
        self.run_with(returncode=1, stderr="Unable to establish IPMI v2 session")
        with self.assertRaises(IPMIError) as caught:
            IPMI().raw_command("0x30")
        self.assertIn("Unable to establish", str(caught.exception))

    def test_a_non_zero_exit_names_the_command_and_code(self):
        self.run_with(returncode=7, stderr="boom")
        with self.assertRaises(IPMIError) as caught:
            IPMI().raw_command("0x30", "0x30")
        message = str(caught.exception)
        self.assertIn("0x30 0x30", message)
        self.assertIn("7", message)

    def test_a_missing_binary_raises(self):
        self.run_with(error=FileNotFoundError("no ipmitool"))
        with self.assertRaises(IPMIError):
            IPMI().raw_command("0x30")

    def test_a_permission_error_raises(self):
        self.run_with(error=PermissionError("/dev/ipmi0"))
        with self.assertRaises(IPMIError):
            IPMI().raw_command("0x30")

    def test_a_timeout_raises(self):
        # A wedged BMC must not park the poll loop behind a bare exception.
        self.run_with(error=subprocess.TimeoutExpired(cmd="ipmitool", timeout=5.0))
        with self.assertRaises(IPMIError):
            IPMI().raw_command("0x30")

    def test_the_original_error_is_chained(self):
        original = FileNotFoundError("no ipmitool")
        self.run_with(error=original)
        with self.assertRaises(IPMIError) as caught:
            IPMI().raw_command("0x30")
        self.assertIs(caught.exception.__cause__, original)


# ---------------------------------------------------------------------------
# Sensor table parsing
# ---------------------------------------------------------------------------
class SensorTableTests(unittest.TestCase):
    """Pure text in, dict out."""

    def parse(self, text=_SENSOR_TABLE):
        return IPMI._parse_sensor_table(text)

    def test_named_sensors_are_read(self):
        table = self.parse()
        self.assertEqual(table["Inlet Temp"], (22.0, "degrees C"))
        self.assertEqual(table["Exhaust Temp"], (34.0, "degrees C"))

    def test_duplicate_names_are_numbered_in_encounter_order(self):
        # Dual-CPU PowerEdge boards report two sensors both called "Temp".
        table = self.parse()
        self.assertEqual(table["Temp"][0], 41.0)
        self.assertEqual(table["Temp #2"][0], 40.0)

    def test_a_third_duplicate_keeps_counting(self):
        text = "Temp | 1.000 | degrees C\nTemp | 2.000 | degrees C\nTemp | 3.000 | degrees C\n"
        self.assertEqual(set(self.parse(text)), {"Temp", "Temp #2", "Temp #3"})

    def test_unreadable_sensors_are_skipped(self):
        # "na" is not a reading, and inventing 0.0 for it would read as cold.
        self.assertNotIn("Voltage 1", self.parse())

    def test_discrete_sensors_are_skipped(self):
        self.assertNotIn("Status", self.parse())

    def test_non_temperature_sensors_are_still_parsed(self):
        # _parse_sensor_table is unit-agnostic; filtering happens above it.
        table = self.parse()
        self.assertEqual(table["Fan1A"], (6000.0, "RPM"))
        self.assertEqual(table["Pwr Consumption"], (154.0, "Watts"))

    def test_lines_without_a_separator_are_ignored(self):
        text = "Some banner line\n" + _SENSOR_TABLE
        self.assertEqual(self.parse(text), self.parse())

    def test_short_rows_are_ignored(self):
        text = "Truncated | 40.000\n" + _SENSOR_TABLE
        self.assertNotIn("Truncated", self.parse(text))

    def test_a_nameless_row_is_ignored(self):
        text = " | 40.000 | degrees C\n"
        self.assertEqual(self.parse(text), {})

    def test_an_empty_value_is_ignored(self):
        text = "Ghost |  | degrees C\n"
        self.assertEqual(self.parse(text), {})

    def test_empty_output_parses_to_nothing(self):
        self.assertEqual(self.parse(""), {})

    def test_whitespace_is_stripped_from_names_and_units(self):
        self.assertIn("Inlet Temp", self.parse())
        self.assertEqual(self.parse()["Inlet Temp"][1], "degrees C")

    def test_negative_readings_are_kept(self):
        text = "Cold Thing | -12.500 | degrees C\n"
        self.assertEqual(self.parse(text)["Cold Thing"][0], -12.5)


class SensorReadingsTests(IPMITestCase):
    def test_readings_drop_the_unit(self):
        self.run_with(stdout=_SENSOR_TABLE)
        readings = IPMI().sensor_readings()
        self.assertEqual(readings["Inlet Temp"], 22.0)
        self.assertEqual(readings["Fan1A"], 6000.0)

    def test_readings_come_from_the_sensor_subcommand(self):
        fake = self.run_with(stdout=_SENSOR_TABLE)
        IPMI().sensor_readings()
        self.assertEqual(fake.argv[1:], ["sensor"])


class TemperatureSensorNameTests(IPMITestCase):
    """The filter exists so a caller building temperature sensors cannot
    accidentally wire one up to a fan tachometer or a voltage rail."""

    def names(self, stdout=_SENSOR_TABLE):
        self.run_with(stdout=stdout)
        return IPMI().temperature_sensor_names()

    def test_every_temperature_sensor_is_listed(self):
        self.assertEqual(
            self.names(), ["Temp", "Temp #2", "Inlet Temp", "Exhaust Temp"],
        )

    def test_fan_speeds_are_excluded(self):
        names = self.names()
        self.assertNotIn("Fan1A", names)
        self.assertNotIn("Fan2A", names)

    def test_power_and_current_are_excluded(self):
        names = self.names()
        self.assertNotIn("Pwr Consumption", names)
        self.assertNotIn("Current 1", names)

    def test_the_unit_match_is_case_insensitive(self):
        self.assertEqual(self.names("Odd Temp | 40.000 | Degrees C\n"), ["Odd Temp"])

    def test_fahrenheit_would_also_be_accepted(self):
        # The filter is on "degree", not on Celsius specifically.
        self.assertEqual(self.names("Odd Temp | 104.000 | degrees F\n"), ["Odd Temp"])

    def test_a_bmc_reporting_nothing_yields_no_names(self):
        self.assertEqual(self.names(""), [])


# ---------------------------------------------------------------------------
# IPMISensor
# ---------------------------------------------------------------------------
class IPMISensorTests(IPMITestCase):
    def test_the_named_sensor_is_returned(self):
        self.run_with(stdout=_SENSOR_TABLE)
        self.assertEqual(IPMISensor(IPMI(), "Exhaust Temp").read(), 34.0)

    def test_the_default_sensor_is_exhaust_temp(self):
        self.run_with(stdout=_SENSOR_TABLE)
        self.assertEqual(IPMISensor(IPMI()).read(), 34.0)

    def test_a_numbered_duplicate_can_be_read(self):
        self.run_with(stdout=_SENSOR_TABLE)
        self.assertEqual(IPMISensor(IPMI(), "Temp #2").read(), 40.0)

    def test_a_missing_sensor_raises(self):
        # A sensor that vanished between discovery and this poll is a failure,
        # not a zero reading.
        self.run_with(stdout=_SENSOR_TABLE)
        with self.assertRaises(IPMIError) as caught:
            IPMISensor(IPMI(), "Nonexistent Temp").read()
        self.assertIn("Nonexistent Temp", str(caught.exception))

    def test_an_ipmitool_failure_propagates(self):
        self.run_with(returncode=1, stderr="BMC unreachable")
        with self.assertRaises(IPMIError):
            IPMISensor(IPMI(), "Exhaust Temp").read()


# ---------------------------------------------------------------------------
# Fan control
# ---------------------------------------------------------------------------
class FanControlTests(unittest.TestCase):
    """The raw byte sequences are Dell-specific and confirmed against real
    iDRAC hardware. These tests pin what we send; only the R730 can confirm
    what it means."""

    def setUp(self):
        self.ipmi = FakeIPMI()
        self.fans = IPMIFanController(self.ipmi)

    def test_manual_control_sends_the_documented_bytes(self):
        self.fans.enable_manual_control()
        self.assertEqual(self.ipmi.commands, [("0x30", "0x30", "0x01", "0x00")])

    def test_automatic_control_sends_the_documented_bytes(self):
        # This is the one that hands the fans back to iDRAC on exit, and it is
        # duplicated as fand.service's ExecStopPost backstop.
        self.fans.enable_automatic_control()
        self.assertEqual(self.ipmi.commands, [("0x30", "0x30", "0x01", "0x01")])

    def test_manual_and_automatic_differ_only_in_the_last_byte(self):
        self.fans.enable_manual_control()
        self.fans.enable_automatic_control()
        manual, automatic = self.ipmi.commands
        self.assertEqual(manual[:3], automatic[:3])
        self.assertNotEqual(manual[3], automatic[3])

    def test_setting_a_speed_enables_manual_control_first(self):
        # Ordering, not just presence: a speed sent while the BMC is still in
        # automatic mode is silently ignored, and the fans stay where iDRAC
        # wants them.
        self.fans.set_speed(70)
        self.assertEqual(
            self.ipmi.commands,
            [
                ("0x30", "0x30", "0x01", "0x00"),
                ("0x30", "0x30", "0x02", "0xff", "0x46"),
            ],
        )

    def test_percentages_are_encoded_as_hex(self):
        for percent, expected in ((0, "0x00"), (5, "0x05"), (50, "0x32"), (100, "0x64")):
            with self.subTest(percent=percent):
                ipmi = FakeIPMI()
                IPMIFanController(ipmi).set_speed(percent)
                self.assertEqual(ipmi.commands[-1][-1], expected)

    def test_a_fractional_percentage_is_truncated(self):
        self.fans.set_speed(70.9)
        self.assertEqual(self.ipmi.commands[-1][-1], "0x46")

    def test_an_over_range_percentage_is_clamped_to_full(self):
        self.fans.set_speed(150)
        self.assertEqual(self.ipmi.commands[-1][-1], "0x64")

    def test_a_negative_percentage_is_clamped_to_zero(self):
        # Never wraps around into a large byte value.
        self.fans.set_speed(-20)
        self.assertEqual(self.ipmi.commands[-1][-1], "0x00")

    def test_every_whole_percentage_encodes_to_a_single_byte(self):
        for percent in range(0, 101):
            with self.subTest(percent=percent):
                ipmi = FakeIPMI()
                IPMIFanController(ipmi).set_speed(percent)
                encoded = ipmi.commands[-1][-1]
                self.assertRegex(encoded, r"^0x[0-9a-f]{2}$")
                self.assertLessEqual(int(encoded, 16), 0x64)

    def test_an_ipmi_failure_propagates(self):
        # The Controller retries and then records the failure; swallowing it
        # here would report a fan speed that was never applied.
        fans = IPMIFanController(FakeIPMI(error=IPMIError("BMC busy")))
        with self.assertRaises(IPMIError):
            fans.set_speed(70)

    def test_a_release_failure_propagates(self):
        fans = IPMIFanController(FakeIPMI(error=IPMIError("BMC busy")))
        with self.assertRaises(IPMIError):
            fans.enable_automatic_control()


if __name__ == "__main__":
    unittest.main()
