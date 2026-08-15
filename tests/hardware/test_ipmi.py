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

# The verbatim `ipmitool sdr type temperature` table from the R730 this daemon
# runs on. Five fields: name, SDR sensor ID, status, entity ID, reading. The two
# CPU sensors are both called "Temp" and are told apart only by their sensor ID.
_SDR_TABLE = """\
Inlet Temp       | 04h | ok  |  7.1 | 19 degrees C
Exhaust Temp     | 01h | ok  |  7.1 | 34 degrees C
Temp             | 0Eh | ok  |  3.1 | 29 degrees C
Temp             | 0Fh | ok  |  3.2 | 35 degrees C
"""

# The same chassis while the BMC is still initialising, which is what fand sees
# when it starts before iDRAC has finished repopulating its SDR after an AC
# power loss. iDRAC words an absent reading differently across versions, so both
# spellings appear here: the parser must not depend on which one it is handed.
_SDR_TABLE_DEGRADED = """\
Inlet Temp       | 04h | ok  |  7.1 | 19 degrees C
Exhaust Temp     | 01h | ns  |  7.1 | No Reading
Temp             | 0Eh | ns  |  3.1 | Disabled
Temp             | 0Fh | ok  |  3.2 | 35 degrees C
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
# SDR temperature table parsing
# ---------------------------------------------------------------------------
class SdrTemperatureTableTests(unittest.TestCase):
    """Pure text in, {name: celsius | None} out."""

    def parse(self, text=_SDR_TABLE):
        return IPMI._parse_sdr_temperature_table(text)

    def test_every_sensor_is_read(self):
        table = self.parse()
        self.assertEqual(table["Inlet Temp"], 19.0)
        self.assertEqual(table["Exhaust Temp"], 34.0)

    def test_duplicate_names_are_numbered_by_sensor_id(self):
        # Dual-CPU PowerEdge boards report two sensors both called "Temp".
        table = self.parse()
        self.assertEqual(table["Temp"], 29.0)       # 0Eh
        self.assertEqual(table["Temp #2"], 35.0)    # 0Fh

    def test_numbering_follows_sensor_id_not_output_order(self):
        # The suffix must be a property of the hardware, not of the order the
        # BMC happened to walk its SDR in.
        rows = _SDR_TABLE.strip().splitlines()
        table = self.parse("\n".join(reversed(rows)) + "\n")
        self.assertEqual(table["Temp"], 29.0)
        self.assertEqual(table["Temp #2"], 35.0)

    def test_an_unreadable_sensor_is_kept_with_no_reading(self):
        # The whole point: absent from the reading is not absent from the
        # chassis. Dropping it here is what silently shrank the sensor set.
        table = self.parse(_SDR_TABLE_DEGRADED)
        self.assertIn("Exhaust Temp", table)
        self.assertIsNone(table["Exhaust Temp"])

    def test_numbering_is_unchanged_when_a_duplicate_is_unreadable(self):
        # "Temp" is 0Eh whether or not 0Eh can be read right now. If an
        # unreadable row stopped consuming its slot, the readable 0Fh sensor
        # would slide into "Temp" and the daemon would report CPU2's
        # temperature under CPU1's name.
        table = self.parse(_SDR_TABLE_DEGRADED)
        self.assertIsNone(table["Temp"])
        self.assertEqual(table["Temp #2"], 35.0)

    def test_both_unreadable_spellings_parse_to_none(self):
        table = self.parse(_SDR_TABLE_DEGRADED)
        self.assertIsNone(table["Exhaust Temp"])    # "No Reading"
        self.assertIsNone(table["Temp"])            # "Disabled"

    def test_the_ns_spelling_also_parses_to_none(self):
        self.assertIsNone(self.parse("Temp | 0Eh | ns | 3.1 | ns\n")["Temp"])

    def test_a_third_duplicate_keeps_counting(self):
        text = (
            "Temp | 0Eh | ok | 3.1 | 1 degrees C\n"
            "Temp | 0Fh | ok | 3.2 | 2 degrees C\n"
            "Temp | 10h | ok | 3.3 | 3 degrees C\n"
        )
        self.assertEqual(set(self.parse(text)), {"Temp", "Temp #2", "Temp #3"})

    def test_fahrenheit_is_converted_to_celsius(self):
        # iDRAC can be configured to report Fahrenheit. Every threshold in
        # config.toml is Celsius, so trusting the number as-is would read 104F
        # as 104C and trip an emergency shutdown on an idle machine.
        table = self.parse("Odd Temp | 04h | ok | 7.1 | 104 degrees F\n")
        self.assertAlmostEqual(table["Odd Temp"], 40.0)

    def test_an_unknown_unit_has_no_reading(self):
        # Better no reading than a number in units we cannot name.
        self.assertIsNone(self.parse("Odd | 04h | ok | 7.1 | 42 kelvin\n")["Odd"])

    def test_the_unit_match_is_case_insensitive(self):
        table = self.parse("Odd | 04h | ok | 7.1 | 40 Degrees C\n")
        self.assertEqual(table["Odd"], 40.0)

    def test_negative_readings_are_kept(self):
        table = self.parse("Cold Thing | 04h | ok | 7.1 | -12 degrees C\n")
        self.assertEqual(table["Cold Thing"], -12.0)

    def test_fractional_readings_are_kept(self):
        table = self.parse("Precise | 04h | ok | 7.1 | 23.5 degrees C\n")
        self.assertEqual(table["Precise"], 23.5)

    def test_lines_without_a_separator_are_ignored(self):
        self.assertEqual(self.parse("Some banner line\n" + _SDR_TABLE), self.parse())

    def test_short_rows_are_ignored(self):
        self.assertNotIn("Truncated", self.parse("Truncated | 04h\n" + _SDR_TABLE))

    def test_a_nameless_row_is_ignored(self):
        self.assertEqual(self.parse(" | 04h | ok | 7.1 | 19 degrees C\n"), {})

    def test_empty_output_parses_to_nothing(self):
        self.assertEqual(self.parse(""), {})

    def test_whitespace_is_stripped_from_names(self):
        self.assertIn("Inlet Temp", self.parse())


class TemperatureReadingsTests(IPMITestCase):
    def test_readings_come_from_the_sdr_temperature_subcommand(self):
        fake = self.run_with(stdout=_SDR_TABLE)
        IPMI().temperature_readings()
        self.assertEqual(fake.argv[1:], ["sdr", "type", "temperature"])

    def test_readings_are_returned_by_name(self):
        self.run_with(stdout=_SDR_TABLE)
        self.assertEqual(IPMI().temperature_readings()["Inlet Temp"], 19.0)


class SdrCacheTests(IPMITestCase):
    """One BMC query per cycle, not one per sensor.

    Each IPMISensor used to trigger its own full table walk. On a chassis with
    several sensors that multiplies BMC latency by the sensor count, inside the
    same cycle that has to kick a 60s watchdog afterwards.
    """

    def test_the_bmc_is_queried_once_across_many_reads(self):
        fake = self.run_with(stdout=_SDR_TABLE)
        ipmi = IPMI()
        for name in ("Inlet Temp", "Exhaust Temp", "Temp", "Temp #2"):
            IPMISensor(ipmi, name).read()
        self.assertEqual(len(fake.calls), 1)

    def test_begin_cycle_forces_a_fresh_query(self):
        # Cached readings must not outlive the cycle that produced them.
        fake = self.run_with(stdout=_SDR_TABLE)
        ipmi = IPMI()
        ipmi.temperature_readings()
        ipmi.begin_cycle()
        ipmi.temperature_readings()
        self.assertEqual(len(fake.calls), 2)

    def test_a_failed_query_is_not_cached(self):
        # Otherwise the retry decorator would re-examine a cached failure three
        # times over without ever asking the BMC again.
        fake = self.run_with(returncode=1, stderr="BMC unreachable")
        ipmi = IPMI()
        for _ in range(3):
            with self.assertRaises(IPMIError):
                ipmi.temperature_readings()
        self.assertEqual(len(fake.calls), 3)

    def test_a_cycle_sees_one_consistent_snapshot(self):
        # All of a cycle's readings come from the same instant, so two sensors
        # can never be compared across different BMC samples.
        self.run_with(stdout=_SDR_TABLE)
        ipmi = IPMI()
        self.assertEqual(ipmi.temperature_readings(), ipmi.temperature_readings())


class TemperatureSensorNameTests(IPMITestCase):
    """Names drive discovery, so this is the list the daemon ends up driving."""

    def names(self, stdout=_SDR_TABLE):
        self.run_with(stdout=stdout)
        return IPMI().temperature_sensor_names()

    def test_every_temperature_sensor_is_listed(self):
        self.assertEqual(
            self.names(), ["Inlet Temp", "Exhaust Temp", "Temp", "Temp #2"],
        )

    def test_unreadable_sensors_are_still_listed(self):
        # A sensor the BMC cannot read yet still exists and still has to be
        # discovered, or it never gets the chance to recover.
        self.assertEqual(
            self.names(_SDR_TABLE_DEGRADED),
            ["Inlet Temp", "Exhaust Temp", "Temp", "Temp #2"],
        )

    def test_a_bmc_reporting_nothing_yields_no_names(self):
        self.assertEqual(self.names(""), [])


# ---------------------------------------------------------------------------
# IPMISensor
# ---------------------------------------------------------------------------
class IPMISensorTests(IPMITestCase):
    def test_the_named_sensor_is_returned(self):
        self.run_with(stdout=_SDR_TABLE)
        self.assertEqual(IPMISensor(IPMI(), "Exhaust Temp").read(), 34.0)

    def test_the_default_sensor_is_exhaust_temp(self):
        self.run_with(stdout=_SDR_TABLE)
        self.assertEqual(IPMISensor(IPMI()).read(), 34.0)

    def test_a_numbered_duplicate_can_be_read(self):
        self.run_with(stdout=_SDR_TABLE)
        self.assertEqual(IPMISensor(IPMI(), "Temp #2").read(), 35.0)

    def test_a_missing_sensor_raises(self):
        # A sensor that vanished between discovery and this poll is a failure,
        # not a zero reading.
        self.run_with(stdout=_SDR_TABLE)
        with self.assertRaises(IPMIError) as caught:
            IPMISensor(IPMI(), "Nonexistent Temp").read()
        self.assertIn("Nonexistent Temp", str(caught.exception))

    def test_an_unreadable_sensor_raises(self):
        # Listed by the SDR but with no reading. Raising is what routes it into
        # SensorManager's failed-sensor handling, so it warns once and recovers
        # by itself once the BMC starts reporting it.
        self.run_with(stdout=_SDR_TABLE_DEGRADED)
        with self.assertRaises(IPMIError) as caught:
            IPMISensor(IPMI(), "Exhaust Temp").read()
        self.assertIn("Exhaust Temp", str(caught.exception))

    def test_an_unreadable_sensor_is_not_reported_as_zero(self):
        # Zero would read as ice-cold and idle the fans on a hot chassis.
        self.run_with(stdout=_SDR_TABLE_DEGRADED)
        with self.assertRaises(IPMIError):
            IPMISensor(IPMI(), "Temp").read()

    def test_a_readable_duplicate_is_unaffected_by_an_unreadable_one(self):
        self.run_with(stdout=_SDR_TABLE_DEGRADED)
        self.assertEqual(IPMISensor(IPMI(), "Temp #2").read(), 35.0)

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
