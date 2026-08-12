"""Abstract notification endpoint interface.

Deliberately transport-agnostic: it imports nothing from `utils/http.py`, so a
future endpoint that writes a file or a socket fits the same interface. The
HTTP status classifier below takes primitives for the same reason — an endpoint
that never speaks HTTP simply never calls it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lib.notifications.notification import Notification


class EndpointError(Exception):
    """Delivery failed."""


class TransientEndpointError(EndpointError):
    """Delivery failed in a way another attempt might survive.

    `retry_after` carries a server-supplied delay when there was one; the
    notifier still bounds it by its own backoff cap.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class PermanentEndpointError(EndpointError):
    """Delivery failed in a way retrying cannot fix.

    A rejected token fails identically on every attempt. Without this
    distinction one mistyped credential would burn the whole retry budget, and
    every backoff delay with it, for every job indefinitely.
    """


def raise_for_http_status(status: int, retry_after: float | None, context: str) -> None:
    """Classify an HTTP status for endpoints that speak HTTP.

    2xx returns. 429 and 5xx are transient. Everything else — including 3xx,
    which `utils/http.py` surfaces rather than following — is permanent.
    """
    if 200 <= status < 300:
        return
    if status == 429:
        raise TransientEndpointError(f"{context}: rate limited (429)", retry_after)
    if 500 <= status < 600:
        raise TransientEndpointError(f"{context}: server error ({status})")
    raise PermanentEndpointError(f"{context}: rejected ({status})")


class NotificationEndpoint(ABC):
    """One external service a notification can be delivered to."""

    @property
    @abstractmethod
    def endpoint_type(self) -> str:
        """The `EndpointType` value this implementation serves."""

    @abstractmethod
    def send(self, notification: Notification) -> None:
        """Deliver one notification.

        Raises TransientEndpointError or PermanentEndpointError on failure.
        Must not retry: the notifier owns the retry budget, because it is the
        component that knows what the configuration allows.
        """
