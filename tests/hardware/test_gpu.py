"""Tests for lib/hardware/gpu.py.

The guest agent is faked, so no VM, no socket, and no nvidia-smi are involved.
These GPUs are the reason the daemon exists -- iDRAC cannot see them -- so the
failure paths matter as much as the happy one: a GPU whose temperature cannot be
read must raise, never quietly report something cool.
"""

from __future__ import annotations

import unittest

from lib.hardware.gpu import NVIDIA_SMI_ARGS, NVIDIA_SMI_PATH, GPUReadError, GPUSensor
from lib.utils.qga import ExecResult, QGAError, QGATimeoutError


def _result(stdout="78\n", stderr="", exit_code=0, signal=None) -> ExecResult:
    return ExecResult(
        exit_code=exit_code, signal=signal, stdout=stdout, stderr=stderr,
    )


class FakeQGA:
    """Records the guest-exec request and replays a scripted result."""

    def __init__(self, result=None, error=None):
        self.calls: list[tuple[tuple, dict]] = []
        self._result = result if result is not None else _result()
        self._error = error

    def exec_and_wait(self, *args, **kwargs) -> ExecResult:
        self.calls.append((args, kwargs))
        if self._error is not None:
            raise self._error
        return self._result


class RequestTests(unittest.TestCase):
    def test_nvidia_smi_is_run_with_the_machine_readable_flags(self):
        qga = FakeQGA()
        GPUSensor(qga).read()
        args, _kwargs = qga.calls[0]
        self.assertEqual(args[0], NVIDIA_SMI_PATH)
        self.assertEqual(args[1], NVIDIA_SMI_ARGS)

    def test_the_query_asks_only_for_the_temperature(self):
        # csv,noheader,nounits is what makes the output a bare number.
        self.assertIn("--query-gpu=temperature.gpu", NVIDIA_SMI_ARGS)
        self.assertIn("--format=csv,noheader,nounits", NVIDIA_SMI_ARGS)

    def test_the_configured_timeout_is_passed_through(self):
        qga = FakeQGA()
        GPUSensor(qga, timeout=2.5).read()
        self.assertEqual(qga.calls[0][1]["timeout"], 2.5)

    def test_the_default_timeout_is_five_seconds(self):
        qga = FakeQGA()
        GPUSensor(qga).read()
        self.assertEqual(qga.calls[0][1]["timeout"], 5.0)

    def test_one_read_makes_one_request(self):
        qga = FakeQGA()
        GPUSensor(qga).read()
        self.assertEqual(len(qga.calls), 1)


class ParsingTests(unittest.TestCase):
    def read(self, stdout: str) -> float:
        return GPUSensor(FakeQGA(_result(stdout=stdout))).read()

    def test_a_plain_reading_is_parsed(self):
        self.assertEqual(self.read("78\n"), 78.0)

    def test_a_fractional_reading_is_parsed(self):
        self.assertEqual(self.read("78.5\n"), 78.5)

    def test_surrounding_whitespace_is_ignored(self):
        self.assertEqual(self.read("   78   \n"), 78.0)

    def test_output_without_a_trailing_newline_is_parsed(self):
        self.assertEqual(self.read("78"), 78.0)

    def test_the_first_gpu_wins_on_a_multi_gpu_card(self):
        # An A16 reports one line per GPU. Policy wants the hottest reading
        # overall, but this sensor reports one number; taking the first is the
        # documented behaviour rather than an accident.
        self.assertEqual(self.read("78\n81\n80\n79\n"), 78.0)

    def test_blank_output_raises(self):
        with self.assertRaises(GPUReadError):
            self.read("")

    def test_whitespace_only_output_raises(self):
        with self.assertRaises(GPUReadError):
            self.read("   \n\n")

    def test_unparsable_output_raises(self):
        with self.assertRaises(GPUReadError):
            self.read("N/A\n")

    def test_a_driver_error_message_raises(self):
        # nvidia-smi can exit 0 and still print prose when the driver is
        # confused. That must not become a temperature.
        with self.assertRaises(GPUReadError):
            self.read("Unable to determine the device handle for GPU 0000:3B:00.0\n")

    def test_the_unparsable_output_is_quoted_in_the_error(self):
        with self.assertRaises(GPUReadError) as caught:
            self.read("N/A\n")
        self.assertIn("N/A", str(caught.exception))

    def test_a_negative_reading_is_accepted(self):
        # Nonsense from the driver, but the sensor's job is to report, not to
        # judge. Policy decides what a number means.
        self.assertEqual(self.read("-5\n"), -5.0)


class FailureTests(unittest.TestCase):
    """Every failure has to surface as GPUReadError so SensorManager records a
    sensor failure instead of the poll dying on an unexpected type."""

    def test_a_non_zero_exit_raises(self):
        sensor = GPUSensor(FakeQGA(_result(exit_code=9, stderr="No devices were found")))
        with self.assertRaises(GPUReadError):
            sensor.read()

    def test_the_exit_code_and_stderr_are_reported(self):
        sensor = GPUSensor(FakeQGA(_result(exit_code=9, stderr="No devices were found")))
        with self.assertRaises(GPUReadError) as caught:
            sensor.read()
        message = str(caught.exception)
        self.assertIn("9", message)
        self.assertIn("No devices were found", message)

    def test_a_command_killed_by_a_signal_raises(self):
        # guest-exec reports exit_code=None when the process was signalled.
        sensor = GPUSensor(FakeQGA(_result(exit_code=None, signal=9, stdout="")))
        with self.assertRaises(GPUReadError):
            sensor.read()

    def test_a_non_zero_exit_is_not_parsed_for_a_temperature(self):
        # Stale stdout alongside a failure must not be believed.
        sensor = GPUSensor(FakeQGA(_result(stdout="78\n", exit_code=9)))
        with self.assertRaises(GPUReadError):
            sensor.read()

    def test_a_guest_agent_error_is_wrapped(self):
        sensor = GPUSensor(FakeQGA(error=QGAError("no such command")))
        with self.assertRaises(GPUReadError):
            sensor.read()

    def test_a_guest_agent_timeout_is_wrapped(self):
        # QGATimeoutError subclasses QGAError, so a wedged agent lands here too.
        sensor = GPUSensor(FakeQGA(error=QGATimeoutError("did not exit within 5s")))
        with self.assertRaises(GPUReadError):
            sensor.read()

    def test_the_guest_agent_error_is_chained(self):
        original = QGAError("no such command")
        sensor = GPUSensor(FakeQGA(error=original))
        with self.assertRaises(GPUReadError) as caught:
            sensor.read()
        self.assertIs(caught.exception.__cause__, original)

    def test_a_socket_error_is_not_wrapped(self):
        # Documents current behaviour rather than endorsing it: QGAClient does
        # not convert socket errors into QGAError, so an unreachable guest
        # agent escapes as OSError. SensorManager's broad except still records
        # it as a sensor failure, so the outcome is correct -- but the type is
        # inconsistent with this module's own contract.
        sensor = GPUSensor(FakeQGA(error=ConnectionRefusedError("socket gone")))
        with self.assertRaises(OSError):
            sensor.read()


if __name__ == "__main__":
    unittest.main()
