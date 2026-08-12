"""Tests for lib/utils/retry.py: existing behaviour, cancellable backoff,
and the exception-supplied delay override."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from lib.utils.retry import retry


class _Boom(Exception):
    pass


def _always_fails(counter: list):
    @retry(exceptions=(_Boom,), attempts=3, backoff=0.01)
    def func():
        counter.append(1)
        raise _Boom("nope")

    return func


class ExistingBehaviourTests(unittest.TestCase):
    """The existing callers pass no cancel_event and must be unaffected."""

    def test_returns_on_first_success(self):
        calls = []

        @retry(exceptions=(_Boom,), attempts=3, backoff=0.01)
        def func():
            calls.append(1)
            return "ok"

        self.assertEqual(func(), "ok")
        self.assertEqual(len(calls), 1)

    def test_retries_then_succeeds(self):
        calls = []

        @retry(exceptions=(_Boom,), attempts=3, backoff=0.01)
        def func():
            calls.append(1)
            if len(calls) < 3:
                raise _Boom("not yet")
            return "ok"

        self.assertEqual(func(), "ok")
        self.assertEqual(len(calls), 3)

    def test_raises_last_exception_after_exhausting_attempts(self):
        calls = []
        with self.assertRaises(_Boom):
            _always_fails(calls)()
        self.assertEqual(len(calls), 3)

    def test_uses_time_sleep_without_a_cancel_event(self):
        calls = []
        with patch("lib.utils.retry.time.sleep") as sleep:
            with self.assertRaises(_Boom):
                _always_fails(calls)()
        # Two waits between three attempts, doubling each time.
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [0.01, 0.02])

    def test_backoff_is_capped(self):
        calls = []

        @retry(exceptions=(_Boom,), attempts=4, backoff=1.0, max_backoff=1.5)
        def func():
            calls.append(1)
            raise _Boom("nope")

        with patch("lib.utils.retry.time.sleep") as sleep:
            with self.assertRaises(_Boom):
                func()
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1.0, 1.5, 1.5])

    def test_non_matching_exception_is_not_retried(self):
        calls = []

        @retry(exceptions=(_Boom,), attempts=3, backoff=0.01)
        def func():
            calls.append(1)
            raise ValueError("different")

        with self.assertRaises(ValueError):
            func()
        self.assertEqual(len(calls), 1)


class CancelEventTests(unittest.TestCase):
    def test_preset_event_abandons_remaining_attempts(self):
        calls = []
        cancel = threading.Event()
        cancel.set()

        @retry(exceptions=(_Boom,), attempts=5, backoff=10.0, cancel_event=cancel)
        def func():
            calls.append(1)
            raise _Boom("nope")

        started = time.monotonic()
        with self.assertRaises(_Boom):
            func()
        # The first attempt still runs; only the waits are cancellable.
        self.assertEqual(len(calls), 1)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_never_sleeps_when_cancelled(self):
        cancel = threading.Event()
        cancel.set()

        @retry(exceptions=(_Boom,), attempts=3, backoff=10.0, cancel_event=cancel)
        def func():
            raise _Boom("nope")

        with patch("lib.utils.retry.time.sleep") as sleep:
            with self.assertRaises(_Boom):
                func()
        sleep.assert_not_called()

    def test_event_set_during_backoff_aborts_promptly(self):
        calls = []
        cancel = threading.Event()

        @retry(exceptions=(_Boom,), attempts=5, backoff=10.0, cancel_event=cancel)
        def func():
            calls.append(1)
            raise _Boom("nope")

        threading.Timer(0.05, cancel.set).start()
        started = time.monotonic()
        with self.assertRaises(_Boom):
            func()
        elapsed = time.monotonic() - started
        # Without cancellation this would wait 10s before the second attempt.
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(calls), 1)

    def test_unset_event_behaves_like_normal_retry(self):
        calls = []
        cancel = threading.Event()

        @retry(exceptions=(_Boom,), attempts=3, backoff=0.01, cancel_event=cancel)
        def func():
            calls.append(1)
            if len(calls) < 3:
                raise _Boom("not yet")
            return "ok"

        self.assertEqual(func(), "ok")
        self.assertEqual(len(calls), 3)

    def test_cancellation_still_raises_the_last_exception(self):
        cancel = threading.Event()
        cancel.set()

        @retry(exceptions=(_Boom,), attempts=3, backoff=10.0, cancel_event=cancel)
        def func():
            raise _Boom("the real reason")

        with self.assertRaises(_Boom) as ctx:
            func()
        self.assertIn("the real reason", str(ctx.exception))

    def test_cancellation_does_not_announce_a_retry(self):
        """A cancelled attempt must not log that it is retrying in N seconds."""
        cancel = threading.Event()
        cancel.set()

        @retry(exceptions=(_Boom,), attempts=3, backoff=10.0, cancel_event=cancel)
        def func():
            raise _Boom("nope")

        with self.assertLogs("lib.utils.retry", level="DEBUG") as logs:
            with self.assertRaises(_Boom):
                func()
        self.assertFalse([line for line in logs.output if "retrying in" in line])
        self.assertTrue([line for line in logs.output if "cancelled" in line])



class DelayOverrideTests(unittest.TestCase):
    """Lets the raised exception dictate its own wait, for protocols that say
    how long to hold off (an HTTP 429's Retry-After)."""

    class _Throttled(_Boom):
        def __init__(self, retry_after=None):
            super().__init__("throttled")
            self.retry_after = retry_after

    def _run(self, exc_factory, **retry_kwargs):
        calls = []

        @retry(exceptions=(_Boom,), attempts=3, backoff=1.0, **retry_kwargs)
        def func():
            calls.append(1)
            raise exc_factory()

        with patch("lib.utils.retry.time.sleep") as sleep:
            with self.assertRaises(_Boom):
                func()
        return [c.args[0] for c in sleep.call_args_list]

    def test_override_replaces_the_computed_backoff(self):
        waits = self._run(
            lambda: self._Throttled(7.0),
            delay_override=lambda exc: getattr(exc, "retry_after", None),
        )
        self.assertEqual(waits, [7.0, 7.0])

    def test_none_from_the_hook_falls_back_to_exponential(self):
        waits = self._run(
            lambda: self._Throttled(None),
            delay_override=lambda exc: getattr(exc, "retry_after", None),
        )
        self.assertEqual(waits, [1.0, 2.0])

    def test_override_is_clamped_by_max_backoff(self):
        waits = self._run(
            lambda: self._Throttled(9999.0),
            max_backoff=30.0,
            delay_override=lambda exc: exc.retry_after,
        )
        self.assertEqual(waits, [30.0, 30.0])

    def test_exponential_ramp_advances_independently(self):
        # One server-supplied delay must not reset the sequence.
        seq = [self._Throttled(5.0), self._Throttled(None), self._Throttled(None)]
        calls = []

        @retry(
            exceptions=(_Boom,), attempts=4, backoff=1.0,
            delay_override=lambda exc: exc.retry_after,
        )
        def func():
            calls.append(1)
            raise seq[len(calls) - 1] if len(calls) <= len(seq) else _Boom("x")

        with patch("lib.utils.retry.time.sleep") as sleep:
            with self.assertRaises(_Boom):
                func()
        # 5.0 came from the server; the ramp still went 1 -> 2 -> 4 underneath.
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5.0, 2.0, 4.0])

    def test_absent_hook_is_the_previous_behaviour(self):
        self.assertEqual(self._run(lambda: _Boom("plain")), [1.0, 2.0])


if __name__ == "__main__":
    unittest.main()
