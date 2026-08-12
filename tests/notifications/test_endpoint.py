"""Tests for lib/notifications/endpoint.py."""

from __future__ import annotations

import unittest

from lib.notifications.endpoint import (
    EndpointError,
    NotificationEndpoint,
    PermanentEndpointError,
    TransientEndpointError,
    raise_for_http_status,
)


class ErrorTypeTests(unittest.TestCase):
    def test_both_are_endpoint_errors(self):
        # The notifier catches EndpointError to decide; both must qualify.
        self.assertTrue(issubclass(TransientEndpointError, EndpointError))
        self.assertTrue(issubclass(PermanentEndpointError, EndpointError))

    def test_transient_carries_retry_after(self):
        exc = TransientEndpointError("rate limited", 12.5)
        self.assertEqual(exc.retry_after, 12.5)

    def test_retry_after_defaults_to_none(self):
        self.assertIsNone(TransientEndpointError("boom").retry_after)


class StatusClassificationTests(unittest.TestCase):
    def test_success_statuses_return(self):
        for status in (200, 201, 204, 299):
            with self.subTest(status=status):
                raise_for_http_status(status, None, "test")  # must not raise

    def test_rate_limit_is_transient_with_retry_after(self):
        with self.assertRaises(TransientEndpointError) as ctx:
            raise_for_http_status(429, 30.0, "test")
        self.assertEqual(ctx.exception.retry_after, 30.0)

    def test_rate_limit_without_retry_after(self):
        with self.assertRaises(TransientEndpointError) as ctx:
            raise_for_http_status(429, None, "test")
        self.assertIsNone(ctx.exception.retry_after)

    def test_server_errors_are_transient(self):
        for status in (500, 502, 503, 504, 599):
            with self.subTest(status=status):
                with self.assertRaises(TransientEndpointError):
                    raise_for_http_status(status, None, "test")

    def test_client_errors_are_permanent(self):
        # A rejected token fails identically every attempt; retrying it would
        # burn the whole budget on a request that can never succeed.
        for status in (400, 401, 403, 404, 405, 422):
            with self.subTest(status=status):
                with self.assertRaises(PermanentEndpointError):
                    raise_for_http_status(status, None, "test")

    def test_redirects_are_permanent(self):
        # http.py surfaces 3xx rather than following it; an API that redirects
        # is misconfigured, and retrying will not change that.
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                with self.assertRaises(PermanentEndpointError):
                    raise_for_http_status(status, None, "test")

    def test_context_appears_in_the_message(self):
        with self.assertRaises(PermanentEndpointError) as ctx:
            raise_for_http_status(401, None, "discord")
        self.assertIn("discord", str(ctx.exception))
        self.assertIn("401", str(ctx.exception))


class InterfaceTests(unittest.TestCase):
    def test_cannot_instantiate_the_abstract_base(self):
        with self.assertRaises(TypeError):
            NotificationEndpoint()

    def test_subclass_must_implement_both_members(self):
        class Incomplete(NotificationEndpoint):
            @property
            def endpoint_type(self):
                return "incomplete"

        with self.assertRaises(TypeError):
            Incomplete()

    def test_complete_subclass_works(self):
        class Complete(NotificationEndpoint):
            @property
            def endpoint_type(self):
                return "complete"

            def send(self, notification):
                return None

        self.assertEqual(Complete().endpoint_type, "complete")


if __name__ == "__main__":
    unittest.main()
