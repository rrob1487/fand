"""Tests for lib/utils/http.py.

Runs against a real loopback HTTP server rather than a mocked urlopen, so the
actual urllib path is exercised: request construction, header casing, body
encoding, and status parsing.
"""

from __future__ import annotations

import json
import socket
import time
import unittest

from lib.utils.http import HTTPTransportError, post_json, warn_if_insecure
from tests.support.http_server import LoopbackServerTestCase


class HTTPTestCase(LoopbackServerTestCase):
    @property
    def url(self) -> str:
        return f"{self.origin}/api"


class RequestTests(HTTPTestCase):
    def test_sends_json_body(self):
        post_json(self.url, {"content": "hello"}, timeout=5.0)
        self.assertEqual(json.loads(self.last_request["body"]), {"content": "hello"})

    def test_sends_default_headers(self):
        post_json(self.url, {}, timeout=5.0)
        self.assertEqual(self.last_request["headers"]["content-type"], "application/json")
        self.assertEqual(self.last_request["headers"]["user-agent"], "fand")

    def test_caller_headers_are_added(self):
        post_json(self.url, {}, {"Authorization": "Bot secret"}, timeout=5.0)
        self.assertEqual(self.last_request["headers"]["authorization"], "Bot secret")

    def test_caller_can_override_defaults(self):
        post_json(self.url, {}, {"User-Agent": "fand/custom"}, timeout=5.0)
        self.assertEqual(self.last_request["headers"]["user-agent"], "fand/custom")

    def test_uses_post(self):
        post_json(self.url, {}, timeout=5.0)
        self.assertEqual(self.last_request["path"], "/api")


class StatusTests(HTTPTestCase):
    """Any answer from the server is a response, never an exception. Judging
    which statuses are retryable belongs to the endpoint."""

    def test_success_returns_status_and_body(self):
        self.respond(status=200, body='{"id":"1"}')
        response = post_json(self.url, {}, timeout=5.0)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), {"id": "1"})
        self.assertIsNone(response.retry_after)

    def test_client_and_server_errors_return_normally(self):
        for status in (400, 401, 403, 404, 429, 500, 502, 503):
            with self.subTest(status=status):
                self.respond(status=status, body='{"message":"nope"}')
                response = post_json(self.url, {}, timeout=5.0)
                self.assertEqual(response.status, status)
                self.assertIn("nope", response.body)

    def test_error_body_is_preserved(self):
        self.respond(status=401, body='{"message":"401: Unauthorized"}')
        self.assertIn("Unauthorized", post_json(self.url, {}, timeout=5.0).body)


class RetryAfterTests(HTTPTestCase):
    def test_delta_seconds(self):
        self.respond(status=429, headers={"Retry-After": "5"})
        self.assertEqual(post_json(self.url, {}, timeout=5.0).retry_after, 5.0)

    def test_fractional_delta_seconds(self):
        self.respond(status=429, headers={"Retry-After": "0.75"})
        self.assertEqual(post_json(self.url, {}, timeout=5.0).retry_after, 0.75)

    def test_http_date(self):
        future = time.strftime(
            "%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30),
        )
        self.respond(status=503, headers={"Retry-After": future})
        retry_after = post_json(self.url, {}, timeout=5.0).retry_after
        self.assertIsNotNone(retry_after)
        self.assertGreater(retry_after, 20)
        self.assertLessEqual(retry_after, 31)

    def test_past_http_date_clamps_to_zero(self):
        past = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 60))
        self.respond(status=503, headers={"Retry-After": past})
        self.assertEqual(post_json(self.url, {}, timeout=5.0).retry_after, 0.0)

    def test_unparsable_header_is_ignored(self):
        # Not worth failing a request over; the caller falls back to its backoff.
        self.respond(status=429, headers={"Retry-After": "soon please"})
        self.assertIsNone(post_json(self.url, {}, timeout=5.0).retry_after)

    def test_absent_header(self):
        self.respond(status=429)
        self.assertIsNone(post_json(self.url, {}, timeout=5.0).retry_after)


class RedirectTests(HTTPTestCase):
    def test_redirects_are_not_followed(self):
        """Following one would re-send the Authorization header to the target,
        handing a bearer token to whoever controls the redirect."""
        self.respond(status=302, headers={"Location": "http://127.0.0.1:1/evil"})
        response = post_json(
            self.url, {}, {"Authorization": "Bot secret"}, timeout=5.0,
        )
        self.assertEqual(response.status, 302)


class TransportFailureTests(unittest.TestCase):
    """No response obtained is the only thing that raises."""

    @staticmethod
    def _closed_port() -> int:
        """A port nothing is listening on."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def test_connection_refused(self):
        # Short timeout: Windows retries the connect before reporting refusal,
        # which otherwise costs two seconds per test.
        with self.assertRaises(HTTPTransportError):
            post_json(f"http://127.0.0.1:{self._closed_port()}/api", {}, timeout=0.5)

    def test_invalid_url(self):
        # Request() raises ValueError before any socket work; it must still
        # surface as a transport failure rather than escaping this module.
        with self.assertRaises(HTTPTransportError):
            post_json("not-a-url", {}, timeout=5.0)

    def test_error_message_omits_the_path(self):
        """An endpoint URL can carry a token in its path, and this message is
        logged."""
        url = f"http://127.0.0.1:{self._closed_port()}/webhook/s3cr3t-token"
        with self.assertRaises(HTTPTransportError) as ctx:
            post_json(url, {}, timeout=0.5)
        self.assertNotIn("s3cr3t", str(ctx.exception))


class TimeoutTests(HTTPTestCase):
    def test_slow_response_times_out(self):
        self.respond(delay=2.0)
        with self.assertRaises(HTTPTransportError):
            post_json(self.url, {}, timeout=0.2)


class WarnIfInsecureTests(unittest.TestCase):
    def test_warns_for_http(self):
        with self.assertLogs("lib.utils.http", level="WARNING") as logs:
            warn_if_insecure("http://ha.local:8123")
        self.assertIn("cleartext", logs.output[0])
        self.assertIn("ha.local", logs.output[0])

    def test_silent_for_https(self):
        with self.assertNoLogs("lib.utils.http", level="WARNING"):
            warn_if_insecure("https://ha.local:8123")

    def test_warning_omits_the_path(self):
        with self.assertLogs("lib.utils.http", level="WARNING") as logs:
            warn_if_insecure("http://ha.local/webhook/s3cr3t-token")
        self.assertNotIn("s3cr3t", logs.output[0])

    def test_stateless_across_calls(self):
        # No memo set: each call warns. Callers decide how often to ask, which
        # is what keeps this module free of mutable module-level state.
        with self.assertLogs("lib.utils.http", level="WARNING") as logs:
            warn_if_insecure("http://ha.local")
            warn_if_insecure("http://ha.local")
        self.assertEqual(len(logs.output), 2)


if __name__ == "__main__":
    unittest.main()
