"""Generic retry mechanisms."""

from __future__ import annotations

import logging
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
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry a callable on the given exceptions with exponential backoff.

    The caller decides which exceptions are retryable and how many
    attempts to allow; this decorator does not judge recoverability.
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
                    _log.warning(
                        "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                        func.__qualname__, attempt, attempts, exc, delay,
                    )
                    time.sleep(delay)
                    delay = (
                        min(delay * backoff_multiplier, max_backoff)
                        if max_backoff is not None
                        else delay * backoff_multiplier
                    )
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator
