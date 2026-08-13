"""A loopback QEMU Guest Agent for tests.

The counterpart to http_server.py, and there for the same reason: it lets the
guest-agent client be exercised over a real UNIX socket rather than a mocked
one, so connection handling, request framing, and the read-until-parsable loop
are all covered by the assertions rather than assumed.

POSIX only -- AF_UNIX does not exist on Windows. Every reference to it is inside
a method so this module still imports on any platform; guard the test classes
that use it with `@unittest.skipUnless(hasattr(socket, "AF_UNIX"), ...)`.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
import unittest
from pathlib import Path

_ACCEPT_POLL_SECONDS = 0.02


class _Server:
    """Answers one scripted response per `execute` verb, then closes."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.requests: list[dict] = []
        self.responses: dict[str, object] = {}
        self.hang = False
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(8)
        # Bounds how long shutdown blocks, the same trade-off http_server.py
        # makes with serve_forever's poll_interval.
        self._sock.settimeout(_ACCEPT_POLL_SECONDS)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # -- lifecycle -------------------------------------------------------
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            with conn:
                try:
                    self._handle(conn)
                except OSError:
                    pass  # a client that gave up is expected here

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=5)

    # -- protocol --------------------------------------------------------
    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        raw = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                return
            raw += chunk
            try:
                request = json.loads(raw)
                break
            except json.JSONDecodeError:
                continue

        self.requests.append(request)
        if self.hang:
            # Accept, read, and never answer: a wedged guest agent.
            self._stop.wait(5.0)
            return

        response = self.responses.get(request.get("execute"), {"return": {}})
        if isinstance(response, list):
            # A sequence: consume one per call, repeating the last forever so
            # a polling loop can be scripted without counting its iterations.
            response = response.pop(0) if len(response) > 1 else response[0]
        conn.sendall(json.dumps(response).encode())


class LoopbackQGATestCase(unittest.TestCase):
    """Base class providing a live guest-agent socket on a temporary path.

    Subclasses read `self.socket_path`, script with `self.respond(...)`, and
    assert against `self.requests`.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        # Kept short: UNIX socket paths are limited to about 107 bytes.
        self.socket_path = str(Path(self.tmpdir) / "ga.sock")
        self.server = _Server(self.socket_path)
        self.addCleanup(self.server.close)

    # -- scripting -------------------------------------------------------
    def respond(self, execute: str, **payload) -> None:
        """Answer `execute` with {"return": payload}."""
        self.server.responses[execute] = {"return": payload}

    def respond_with(self, execute: str, response: object) -> None:
        """Answer `execute` with a raw response, or a list to answer in turn."""
        self.server.responses[execute] = response

    def respond_error(self, execute: str, desc: str) -> None:
        self.server.responses[execute] = {"error": {"class": "GenericError", "desc": desc}}

    def never_respond(self) -> None:
        self.server.hang = True

    # -- assertions ------------------------------------------------------
    @property
    def requests(self) -> list[dict]:
        return self.server.requests

    @property
    def last_request(self) -> dict:
        self.assertTrue(self.server.requests, "no request was received")
        return self.server.requests[-1]
