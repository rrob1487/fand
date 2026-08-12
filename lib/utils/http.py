"""Minimal JSON-over-HTTP transport.

Reports what happened and judges nothing: any status the server returns comes
back as an HTTPResponse, including 4xx and 5xx. HTTPTransportError means no
response was obtained at all. Deciding which of those is worth retrying belongs
to the caller, which is the component that knows its own retry budget.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "Content-Type": "application/json",
    # Some APIs reject requests without one. Callers may override it.
    "User-Agent": "fand",
}


class HTTPTransportError(Exception):
    """Raised when no HTTP response was obtained.

    Covers DNS failure, connection refusal, TLS failure, and timeout. A server
    that answers — even with 500 — did not fail at this layer and comes back as
    an HTTPResponse instead.
    """


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: str
    retry_after: float | None


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Turns redirects into ordinary responses instead of following them.

    urllib follows redirects by default and re-sends the request headers to the
    new location. A 302 from an https endpoint to an http one would hand the
    Authorization header to the redirect target in cleartext. API endpoints have
    no legitimate reason to redirect, so a 3xx is surfaced to the caller as-is.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait, from either form RFC 9110 allows.

    Returns None for an absent or unparsable header — a malformed Retry-After
    is not worth failing a request over; the caller falls back to its own
    backoff.
    """
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)  # delta-seconds
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)  # HTTP-date
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    delta = (when - datetime.now(timezone.utc)).total_seconds()
    return max(delta, 0.0)


def warn_if_insecure(url: str) -> None:
    """Warn when a URL would send credentials in cleartext.

    Stateless by design, so callers control how often it fires; endpoints call
    it once when they are constructed. Plain HTTP is permitted rather than
    rejected — a Home Assistant instance on a trusted LAN is a legitimate
    deployment, and refusing it would push operators toward disabling
    certificate verification instead, which is strictly worse.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        _log.warning(
            "endpoint %s uses %s, not https: credentials will be sent in cleartext",
            parts.netloc or "<unknown host>",
            parts.scheme or "no scheme",
        )


def post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    *,
    timeout: float,
) -> HTTPResponse:
    """POST a JSON body and return whatever the server said.

    `timeout` is keyword-only and has no default: a request that connects and
    then never completes would park the calling thread indefinitely, which is
    the failure that turns a bounded queue into an unbounded backlog.

    Raises HTTPTransportError only when no response was obtained. TLS is
    verified using the default context; it is never disabled.
    """
    try:
        # Request() itself raises ValueError on an unusable URL, so it is built
        # inside the try: an operator's typo in an endpoint URL must surface as
        # a transport failure the caller already handles, not as a stray
        # ValueError escaping this module.
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST")
        for key, value in {**_DEFAULT_HEADERS, **(headers or {})}.items():
            request.add_header(key, value)

        with _OPENER.open(request, timeout=timeout) as response:
            return _response_from(response.status, response.headers, response.read())
    except urllib.error.HTTPError as exc:
        # An error status is still an answer from the server, so it is reported
        # rather than raised. The caller decides whether 429 or 503 is worth
        # another attempt.
        with exc:
            return _response_from(exc.code, exc.headers, exc.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # URLError covers DNS and TLS; OSError covers connection failures and
        # socket timeouts; ValueError covers an unusable URL. The message is
        # kept, but never the request body or any header value.
        raise HTTPTransportError(f"POST to {_safe_target(url)} failed: {exc}") from exc


def _response_from(status: int, headers, raw: bytes) -> HTTPResponse:
    return HTTPResponse(
        status=status,
        body=raw.decode("utf-8", errors="replace"),
        retry_after=_parse_retry_after(headers.get("Retry-After")),
    )


def _safe_target(url: str) -> str:
    """scheme://host for log messages.

    Paths are dropped: an endpoint URL can carry a token in its path, and this
    string ends up in an exception message that gets logged.
    """
    parts = urlsplit(url)
    if not parts.netloc:
        return "<invalid url>"
    return f"{parts.scheme}://{parts.netloc}"
