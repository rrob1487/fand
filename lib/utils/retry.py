"""Generic retry mechanisms."""

from __future__ import annotations

import logging
import threading
import time
from functools import wraps
from typing import Callable, TypeVar

T = TypeVar("T")

_log = logging.getLogger(__name__)


def retry(
    *,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    attempts: int = 3,
    backoff: float = 1.0,
    backoff_multiplier: float = 2.0,
    max_backoff: float | None = None,
    cancel_event: threading.Event | None = None,
    delay_override: Callable[[BaseException], float | None] | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry a callable on the given exceptions with exponential backoff.

    The caller decides which exceptions are retryable and how many
    attempts to allow; this decorator does not judge recoverability.

    When `cancel_event` is supplied, backoff waits on it instead of sleeping,
    and remaining attempts are abandoned once it is set. Without it, a caller
    parked in `time.sleep()` cannot observe a shutdown request, so teardown
    either waits out the full backoff or abandons the thread mid-flight.
    Attempts themselves are never skipped — only the waits between them.

    `delay_override` lets the raised exception dictate its own wait, for
    protocols that say how long to hold off — an HTTP 429's Retry-After, say.
    It is consulted per attempt and its result is still clamped by
    `max_backoff`. The exponential sequence advances independently, so one
    server-supplied delay does not reset the ramp. This stays generic: the
    decorator asks a callable and learns nothing about the exception type.
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs) -> T:
            delay = backoff
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == attempts:
                        break
                    # Checked before the log line so we never announce a retry
                    # we are about to abandon.
                    if cancel_event is not None and cancel_event.is_set():
                        _log.debug(
                            "%s cancelled after attempt %d/%d",
                            func.__qualname__, attempt, attempts,
                        )
                        break
                    wait = delay
                    if delay_override is not None:
                        override = delay_override(exc)
                        if override is not None:
                            wait = (
                                min(override, max_backoff)
                                if max_backoff is not None
                                else override
                            )
                    _log.warning(
                        "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                        func.__qualname__, attempt, attempts, exc, wait,
                    )
                    if cancel_event is not None:
                        # wait() returns True only if the event became set.
                        if cancel_event.wait(wait):
                            _log.debug(
                                "%s cancelled during backoff", func.__qualname__,
                            )
                            break
                    else:
                        time.sleep(wait)
                    delay = (
                        min(delay * backoff_multiplier, max_backoff)
                        if max_backoff is not None
                        else delay * backoff_multiplier
                    )
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
