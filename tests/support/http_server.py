"""A loopback HTTP server for tests.

Lets the transport and the endpoints be exercised over real HTTP rather than a
mocked urlopen, so request construction, header casing, body encoding, and
status parsing are all covered — and the assertions are made against the JSON a
real service would actually receive.
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - name mandated by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8")
        self.server.requests.append(
            {
                "path": self.path,
                "body": raw,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            }
        )

        spec = self.server.response_for(self.path)
        if spec.get("delay"):
            time.sleep(spec["delay"])

        body = spec.get("body", "{}").encode("utf-8")
        self.send_response(spec.get("status", 200))
        for key, value in spec.get("headers", {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # keep test output clean


class _Server(ThreadingHTTPServer):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.requests: list[dict] = []
        self.default_response: dict = {}
        self.path_responses: dict[str, dict] = {}

    def response_for(self, path: str) -> dict:
        return self.path_responses.get(path, self.default_response)

    def handle_error(self, request, client_address):
        # A client that closes without reading the body (the redirect test)
        # is expected behaviour here, not something to print a traceback for.
        pass


class LoopbackServerTestCase(unittest.TestCase):
    """Base class providing a live HTTP server on 127.0.0.1.

    Subclasses read `self.url` and call `self.respond(...)` to script the next
    response, then assert against `self.requests`.
    """

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(("127.0.0.1", 0), _Handler)
        cls.port = cls.server.server_address[1]
        cls.origin = f"http://127.0.0.1:{cls.port}"
        # serve_forever polls its shutdown flag on this interval, so it also
        # bounds how long tearDownClass blocks. The 0.5s default costs half a
        # second per test class, which dominates the suite once there are many.
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.server.requests.clear()
        self.server.default_response = {}
        self.server.path_responses.clear()

    # -- scripting -------------------------------------------------------
    def respond(self, **spec):
        """Set the response every path returns."""
        self.server.default_response = spec

    def respond_to(self, path: str, **spec):
        """Set the response one specific path returns."""
        self.server.path_responses[path] = spec

    # -- assertions ------------------------------------------------------
    @property
    def requests(self) -> list[dict]:
        return self.server.requests

    @property
    def last_request(self) -> dict:
        self.assertTrue(self.server.requests, "no request was received")
        return self.server.requests[-1]

    def last_json(self) -> dict:
        return json.loads(self.last_request["body"])
