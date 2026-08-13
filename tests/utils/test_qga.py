"""Tests for lib/utils/qga.py.

Split by what genuinely needs a UNIX socket. Everything above `_call` -- request
construction, the guest-exec polling loop, base64 decoding, the timeout -- is
real code exercised on any platform by substituting `_call`. Only the socket
itself needs AF_UNIX, and those tests drive a real guest agent from
tests/support/qga_server.py and skip on Windows.
"""

from __future__ import annotations

import base64
import json
import socket
import unittest
from unittest.mock import patch

from lib.utils.qga import (
    ExecResult,
    QGAClient,
    QGAError,
    QGATimeoutError,
    _decode_b64,
)
from tests.support.qga_server import LoopbackQGATestCase

_POSIX_ONLY = unittest.skipUnless(
    hasattr(socket, "AF_UNIX"), "AF_UNIX sockets are POSIX-only",
)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class ScriptedQGAClient(QGAClient):
    """A real QGAClient with only the socket write replaced.

    Substituting `_call` rather than faking the whole client keeps guest_exec,
    guest_exec_status, and exec_and_wait under test on every platform.
    """

    def __init__(self, *responses, socket_path="/run/qemu/test-ga.sock", **kwargs):
        super().__init__(socket_path, **kwargs)
        self.requests: list[tuple[str, dict | None]] = []
        self._responses = list(responses) or [{}]

    def _call(self, execute: str, arguments: dict | None = None) -> dict:
        self.requests.append((execute, arguments))
        # The last response repeats forever, so a polling loop can be scripted
        # without counting how many times it will go round.
        response = (
            self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response


class ChunkedSocket:
    """A socket-shaped stub that hands back recv() chunks in order."""

    def __init__(self, *chunks: bytes):
        self.remaining = list(chunks)

    def recv(self, _size: int) -> bytes:
        return self.remaining.pop(0) if self.remaining else b""


# ---------------------------------------------------------------------------
# Base64 decoding
# ---------------------------------------------------------------------------
class DecodeTests(unittest.TestCase):
    def test_a_missing_stream_decodes_to_empty(self):
        # guest-exec-status omits out-data entirely when there was no output.
        self.assertEqual(_decode_b64(None), "")

    def test_an_empty_stream_decodes_to_empty(self):
        self.assertEqual(_decode_b64(""), "")

    def test_text_round_trips(self):
        self.assertEqual(_decode_b64(_b64("78\n")), "78\n")

    def test_multiline_text_round_trips(self):
        self.assertEqual(_decode_b64(_b64("78\n81\n")), "78\n81\n")

    def test_undecodable_bytes_are_replaced_rather_than_raising(self):
        # A guest emitting non-UTF-8 must not take the poll loop down.
        payload = base64.b64encode(b"\xff\xfe temp").decode()
        self.assertIn("temp", _decode_b64(payload))


# ---------------------------------------------------------------------------
# Response framing
# ---------------------------------------------------------------------------
class ResponseFramingTests(unittest.TestCase):
    """The read loop ends when the buffer parses as JSON, because qemu-ga does
    not frame its replies with a length."""

    def read(self, *chunks: bytes) -> tuple[bytes, ChunkedSocket]:
        sock = ChunkedSocket(*chunks)
        return QGAClient("/unused")._read_response(sock), sock

    def test_a_single_chunk_reply_is_returned(self):
        raw, _ = self.read(b'{"return": {"pid": 1234}}')
        self.assertEqual(json.loads(raw), {"return": {"pid": 1234}})

    def test_a_reply_split_across_chunks_is_reassembled(self):
        raw, _ = self.read(b'{"return": {"pi', b'd": 1234}}')
        self.assertEqual(json.loads(raw), {"return": {"pid": 1234}})

    def test_a_reply_split_into_many_chunks_is_reassembled(self):
        payload = b'{"return": {"pid": 1234, "exited": true}}'
        raw, _ = self.read(*(payload[i:i + 3] for i in range(0, len(payload), 3)))
        self.assertEqual(json.loads(raw), json.loads(payload))

    def test_reading_stops_as_soon_as_the_reply_parses(self):
        # Not draining the socket matters: the next call opens a new connection
        # and must not inherit a half-read one.
        _, sock = self.read(b'{"return": {}}', b'{"unread": true}')
        self.assertEqual(sock.remaining, [b'{"unread": true}'])

    def test_a_peer_that_closes_without_replying_returns_nothing(self):
        raw, _ = self.read()
        self.assertEqual(raw, b"")


# ---------------------------------------------------------------------------
# guest-exec
# ---------------------------------------------------------------------------
class GuestExecTests(unittest.TestCase):
    def test_the_pid_is_returned(self):
        client = ScriptedQGAClient({"pid": 1234})
        self.assertEqual(client.guest_exec("/usr/bin/nvidia-smi"), 1234)

    def test_the_verb_and_path_are_sent(self):
        client = ScriptedQGAClient({"pid": 1})
        client.guest_exec("/usr/bin/nvidia-smi")
        execute, arguments = client.requests[0]
        self.assertEqual(execute, "guest-exec")
        self.assertEqual(arguments["path"], "/usr/bin/nvidia-smi")

    def test_output_capture_is_requested_by_default(self):
        # Without it the guest runs the command and discards the temperature.
        client = ScriptedQGAClient({"pid": 1})
        client.guest_exec("/usr/bin/nvidia-smi")
        self.assertIs(client.requests[0][1]["capture-output"], True)

    def test_output_capture_can_be_disabled(self):
        client = ScriptedQGAClient({"pid": 1})
        client.guest_exec("/usr/bin/true", capture_output=False)
        self.assertIs(client.requests[0][1]["capture-output"], False)

    def test_arguments_are_forwarded(self):
        client = ScriptedQGAClient({"pid": 1})
        client.guest_exec("/usr/bin/nvidia-smi", ["--query-gpu=temperature.gpu"])
        self.assertEqual(
            client.requests[0][1]["arg"], ["--query-gpu=temperature.gpu"],
        )

    def test_the_argument_key_is_omitted_when_there_are_none(self):
        # qemu-ga rejects an empty arg list on some versions.
        client = ScriptedQGAClient({"pid": 1})
        client.guest_exec("/usr/bin/true")
        self.assertNotIn("arg", client.requests[0][1])

    def test_an_empty_argument_list_is_also_omitted(self):
        client = ScriptedQGAClient({"pid": 1})
        client.guest_exec("/usr/bin/true", [])
        self.assertNotIn("arg", client.requests[0][1])

    def test_arguments_are_copied_not_aliased(self):
        args = ["--query-gpu=temperature.gpu"]
        client = ScriptedQGAClient({"pid": 1})
        client.guest_exec("/usr/bin/nvidia-smi", args)
        args.append("--mutated")
        self.assertEqual(len(client.requests[0][1]["arg"]), 1)

    def test_a_reply_without_a_pid_raises(self):
        # Better than returning None and polling status for it forever.
        client = ScriptedQGAClient({})
        with self.assertRaises(KeyError):
            client.guest_exec("/usr/bin/true")

    def test_status_is_requested_for_the_given_pid(self):
        client = ScriptedQGAClient({"exited": True})
        client.guest_exec_status(4321)
        self.assertEqual(client.requests[0], ("guest-exec-status", {"pid": 4321}))


# ---------------------------------------------------------------------------
# exec_and_wait
# ---------------------------------------------------------------------------
class ExecAndWaitTests(unittest.TestCase):
    """The polling loop. The clock is patched so no test spends real time."""

    def setUp(self):
        self.clock = 1000.0
        monotonic = patch("lib.utils.qga.time.monotonic", lambda: self.clock)
        monotonic.start()
        self.addCleanup(monotonic.stop)

        def advance(seconds):
            self.clock += seconds

        sleep = patch("lib.utils.qga.time.sleep", advance)
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_a_command_that_already_exited_is_returned_immediately(self):
        client = ScriptedQGAClient(
            {"pid": 1}, {"exited": True, "exitcode": 0, "out-data": _b64("78\n")},
        )
        result = client.exec_and_wait("/usr/bin/nvidia-smi")
        self.assertEqual(result, ExecResult(exit_code=0, signal=None, stdout="78\n", stderr=""))

    def test_polling_continues_until_the_command_exits(self):
        client = ScriptedQGAClient(
            {"pid": 1},
            {"exited": False},
            {"exited": False},
            {"exited": True, "exitcode": 0, "out-data": _b64("78\n")},
        )
        self.assertEqual(client.exec_and_wait("/usr/bin/nvidia-smi").stdout, "78\n")
        # One guest-exec plus three guest-exec-status.
        self.assertEqual(len(client.requests), 4)

    def test_both_streams_are_decoded(self):
        client = ScriptedQGAClient(
            {"pid": 1},
            {
                "exited": True,
                "exitcode": 2,
                "out-data": _b64("partial\n"),
                "err-data": _b64("No devices were found\n"),
            },
        )
        result = client.exec_and_wait("/usr/bin/nvidia-smi")
        self.assertEqual(result.stdout, "partial\n")
        self.assertEqual(result.stderr, "No devices were found\n")

    def test_missing_streams_become_empty_strings(self):
        client = ScriptedQGAClient({"pid": 1}, {"exited": True, "exitcode": 0})
        result = client.exec_and_wait("/usr/bin/true")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")

    def test_a_signalled_command_reports_the_signal(self):
        client = ScriptedQGAClient({"pid": 1}, {"exited": True, "signal": 9})
        result = client.exec_and_wait("/usr/bin/nvidia-smi")
        self.assertIsNone(result.exit_code)
        self.assertEqual(result.signal, 9)

    def test_a_non_zero_exit_code_is_reported_not_raised(self):
        # Classifying the exit code belongs to the caller, not here.
        client = ScriptedQGAClient({"pid": 1}, {"exited": True, "exitcode": 9})
        self.assertEqual(client.exec_and_wait("/usr/bin/nvidia-smi").exit_code, 9)

    def test_a_command_that_never_exits_times_out(self):
        client = ScriptedQGAClient({"pid": 1}, {"exited": False})
        with self.assertRaises(QGATimeoutError):
            client.exec_and_wait("/usr/bin/nvidia-smi", timeout=1.0, poll_interval=0.2)

    def test_the_timeout_message_names_the_command_and_pid(self):
        client = ScriptedQGAClient({"pid": 4321}, {"exited": False})
        with self.assertRaises(QGATimeoutError) as caught:
            client.exec_and_wait("/usr/bin/nvidia-smi", timeout=1.0, poll_interval=0.2)
        message = str(caught.exception)
        self.assertIn("4321", message)
        self.assertIn("nvidia-smi", message)

    def test_a_timeout_is_a_guest_agent_error(self):
        # GPUSensor catches QGAError; a timeout must be caught by that clause.
        self.assertTrue(issubclass(QGATimeoutError, QGAError))

    def test_the_timeout_is_bounded_by_the_deadline_not_the_attempt_count(self):
        client = ScriptedQGAClient({"pid": 1}, {"exited": False})
        with self.assertRaises(QGATimeoutError):
            client.exec_and_wait("/usr/bin/nvidia-smi", timeout=1.0, poll_interval=0.2)
        # 1.0s at 0.2s per poll: the loop stops on the clock, and never spins
        # unbounded when poll_interval is small.
        self.assertLessEqual(len(client.requests), 8)
        self.assertAlmostEqual(self.clock, 1001.0, places=6)

    def test_a_guest_agent_error_propagates(self):
        client = ScriptedQGAClient(QGAError("command not found"))
        with self.assertRaises(QGAError):
            client.exec_and_wait("/usr/bin/nvidia-smi")

    def test_an_error_partway_through_polling_propagates(self):
        client = ScriptedQGAClient(
            {"pid": 1}, {"exited": False}, QGAError("agent restarted"),
        )
        with self.assertRaises(QGAError):
            client.exec_and_wait("/usr/bin/nvidia-smi", timeout=5.0, poll_interval=0.2)


# ---------------------------------------------------------------------------
# The socket itself
# ---------------------------------------------------------------------------
@_POSIX_ONLY
class SocketTests(LoopbackQGATestCase):
    """Driven against a real guest agent on a real UNIX socket."""

    def client(self, **kwargs) -> QGAClient:
        return QGAClient(self.socket_path, **kwargs)

    def test_a_request_reaches_the_agent(self):
        self.respond("guest-exec", pid=1234)
        self.client().guest_exec("/usr/bin/nvidia-smi", ["--flag"])
        self.assertEqual(
            self.last_request,
            {
                "execute": "guest-exec",
                "arguments": {
                    "path": "/usr/bin/nvidia-smi",
                    "capture-output": True,
                    "arg": ["--flag"],
                },
            },
        )

    def test_the_reply_is_parsed(self):
        self.respond("guest-exec", pid=1234)
        self.assertEqual(self.client().guest_exec("/usr/bin/true"), 1234)

    def test_a_request_without_arguments_omits_the_key(self):
        self.respond_with("guest-ping", {"return": {}})
        self.client()._call("guest-ping")
        self.assertNotIn("arguments", self.last_request)

    def test_an_error_reply_raises(self):
        self.respond_error("guest-exec", "Guest agent command failed")
        with self.assertRaises(QGAError) as caught:
            self.client().guest_exec("/usr/bin/true")
        self.assertIn("Guest agent command failed", str(caught.exception))

    def test_a_reply_with_no_return_value_is_an_empty_dict(self):
        self.respond_with("guest-ping", {})
        self.assertEqual(self.client()._call("guest-ping"), {})

    def test_a_full_exec_and_wait_round_trip(self):
        self.respond("guest-exec", pid=1234)
        self.respond_with(
            "guest-exec-status",
            [
                {"return": {"exited": False}},
                {"return": {"exited": True, "exitcode": 0, "out-data": _b64("78\n")}},
            ],
        )
        result = self.client().exec_and_wait(
            "/usr/bin/nvidia-smi", poll_interval=0.0, timeout=5.0,
        )
        self.assertEqual(result.stdout, "78\n")
        self.assertEqual(result.exit_code, 0)

    def test_each_call_opens_its_own_connection(self):
        self.respond("guest-exec", pid=1)
        self.respond_with("guest-exec-status", {"return": {"exited": True, "exitcode": 0}})
        client = self.client()
        client.exec_and_wait("/usr/bin/true", poll_interval=0.0, timeout=5.0)
        self.assertEqual(len(self.requests), 2)

    def test_a_missing_socket_raises_an_os_error(self):
        # Documents current behaviour: a VM that is simply not running fails
        # fast, but as OSError rather than QGAError. GPUSensor only catches
        # QGAError, so this reaches SensorManager's broad handler instead.
        client = QGAClient(self.socket_path + ".absent")
        with self.assertRaises(OSError):
            client.guest_exec("/usr/bin/true")

    def test_a_wedged_agent_raises_rather_than_hanging_forever(self):
        # Also OSError rather than QGAError, for the same reason. The point
        # here is that the socket timeout is honoured at all: without it the
        # poll loop would block indefinitely on one bad VM.
        self.never_respond()
        with self.assertRaises(OSError):
            self.client(timeout=0.2).guest_exec("/usr/bin/true")


if __name__ == "__main__":
    unittest.main()
