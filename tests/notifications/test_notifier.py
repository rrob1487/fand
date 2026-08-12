"""Tests for lib/notifications/notifier.py.

Scheduling is exercised without starting a thread and with the clock patched,
so it is deterministic and instant. Worker behaviour uses a real thread and
waits on an Event the fake endpoint sets, never a sleep.
"""

from __future__ import annotations

import queue
import threading
import time
import unittest
from unittest.mock import patch

from lib.notifications.endpoint import (
    NotificationEndpoint,
    PermanentEndpointError,
    TransientEndpointError,
)
from lib.notifications.notification import Notification, SensorReading
from lib.notifications.notifier import Notifier
from lib.notifications.trigger import GeneralTrigger, ThresholdTrigger, Trigger

_DEADLINE = 5.0


def _notification(*readings, **overrides) -> Notification:
    defaults = dict(
        timestamp=1_700_000_000.0,
        readings=tuple(
            SensorReading(name=n, value_c=v, timestamp=1_700_000_000.0)
            for n, v in readings
        ),
        fan_speed_percent=70.0,
        operating_mode="RUNNING",
        alarms=(),
        last_command_ok=True,
    )
    return Notification(**{**defaults, **overrides})


class FakeEndpoint(NotificationEndpoint):
    """Records deliveries and can be scripted to fail."""

    def __init__(self, failures=(), block=False):
        self.sent = []
        self.calls = 0
        self.delivered = threading.Event()
        self.released = threading.Event()
        self._failures = list(failures)
        self._block = block

    @property
    def endpoint_type(self) -> str:
        return "fake"

    def send(self, notification: Notification) -> None:
        self.calls += 1
        if self._block:
            # Simulates a wedged endpoint: blocks until the test frees it.
            self.delivered.set()
            self.released.wait(_DEADLINE)
            return
        if self._failures:
            raise self._failures.pop(0)
        self.sent.append(notification)
        self.delivered.set()


class ExplodingTrigger(Trigger):
    def __init__(self):
        super().__init__(None)

    def is_active(self, notification):
        raise RuntimeError("trigger is broken")


def _notifier(endpoint=None, trigger=None, **kwargs) -> Notifier:
    params = dict(
        name="Test Notifier",
        endpoint=endpoint or FakeEndpoint(),
        trigger=trigger or GeneralTrigger(None),
        interval_seconds=60.0,
        queue_size=10,
        max_attempts=3,
        retry_backoff_seconds=0.001,
    )
    params.update(kwargs)
    return Notifier(**params)


def _drain(notifier) -> list:
    """Every queued notification, oldest first."""
    jobs = []
    while True:
        try:
            jobs.append(notifier._queue.get_nowait())
        except queue.Empty:
            return jobs


class WorkerTestCase(unittest.TestCase):
    """Base for tests that start a real worker.

    Shortens the idle poll so stop() does not wait out the production 0.5s
    interval on every test. It governs shutdown responsiveness, not
    correctness, so no assertion depends on its value.
    """

    def setUp(self):
        patcher = patch("lib.notifications.notifier._QUEUE_POLL_SECONDS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)


class SchedulingTests(unittest.TestCase):
    """No thread, patched clock: the schedule is pure producer-side logic."""

    def setUp(self):
        self.clock = 1000.0
        patcher = patch("lib.notifications.notifier.time.monotonic", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_rising_edge_fires_immediately(self):
        n = _notifier(trigger=ThresholdTrigger(None, 80.0))
        n.offer(_notification(("CPU1 Temp", 85.0)))
        self.assertEqual(len(_drain(n)), 1)

    def test_no_second_job_inside_the_interval(self):
        n = _notifier(trigger=ThresholdTrigger(None, 80.0), interval_seconds=60.0)
        n.offer(_notification(("CPU1 Temp", 85.0)))
        _drain(n)
        self.clock += 59.0
        n.offer(_notification(("CPU1 Temp", 85.0)))
        self.assertEqual(len(_drain(n)), 0)

    def test_fires_again_once_the_interval_elapses(self):
        n = _notifier(trigger=ThresholdTrigger(None, 80.0), interval_seconds=60.0)
        n.offer(_notification(("CPU1 Temp", 85.0)))
        _drain(n)
        self.clock += 60.0
        n.offer(_notification(("CPU1 Temp", 85.0)))
        self.assertEqual(len(_drain(n)), 1)

    def test_falling_inactive_rearms_the_rising_edge(self):
        # Dropping below and crossing again fires at once, without waiting out
        # an interval that elapsed while the notifier was inactive.
        n = _notifier(trigger=ThresholdTrigger(None, 80.0), interval_seconds=60.0)
        n.offer(_notification(("CPU1 Temp", 85.0)))
        _drain(n)
        self.clock += 1.0
        n.offer(_notification(("CPU1 Temp", 70.0)))
        self.clock += 1.0
        n.offer(_notification(("CPU1 Temp", 85.0)))
        self.assertEqual(len(_drain(n)), 1)

    def test_inactive_never_queues(self):
        n = _notifier(trigger=ThresholdTrigger(None, 80.0))
        for _ in range(5):
            n.offer(_notification(("CPU1 Temp", 20.0)))
            self.clock += 60.0
        self.assertEqual(len(_drain(n)), 0)

    def test_general_trigger_fires_first_cycle_then_on_interval(self):
        n = _notifier(trigger=GeneralTrigger(None), interval_seconds=30.0)
        n.offer(_notification(("CPU1 Temp", 20.0)))
        self.assertEqual(len(_drain(n)), 1)
        self.clock += 29.0
        n.offer(_notification(("CPU1 Temp", 20.0)))
        self.assertEqual(len(_drain(n)), 0)
        self.clock += 1.0
        n.offer(_notification(("CPU1 Temp", 20.0)))
        self.assertEqual(len(_drain(n)), 1)

    def test_general_trigger_fires_without_readings(self):
        n = _notifier(trigger=GeneralTrigger(None))
        n.offer(_notification())
        self.assertEqual(len(_drain(n)), 1)

    def test_delivery_failure_does_not_shift_the_schedule(self):
        """Interval is the queueing interval, not the delivery interval."""
        endpoint = FakeEndpoint(failures=[TransientEndpointError("boom")] * 9)
        n = _notifier(endpoint=endpoint, interval_seconds=60.0)
        n.offer(_notification(("CPU1 Temp", 20.0)))
        due_after_first = n._next_due
        n.start()
        self.addCleanup(n.stop, 1.0)
        time.sleep(0.05)
        self.assertEqual(n._next_due, due_after_first)
        self.clock += 60.0
        n.offer(_notification(("CPU1 Temp", 20.0)))
        self.assertEqual(n._next_due, due_after_first + 60.0)


class QueueOverflowTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        patcher = patch("lib.notifications.notifier.time.monotonic", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fill(self, notifier, count, start=0):
        for i in range(count):
            notifier.offer(_notification(("CPU1 Temp", float(start + i))))
            self.clock += 60.0

    def test_queue_holds_up_to_capacity(self):
        n = _notifier(queue_size=3)
        self._fill(n, 3)
        self.assertEqual(len(_drain(n)), 3)

    def test_overflow_drops_the_oldest_and_keeps_the_newest(self):
        n = _notifier(queue_size=3)
        self._fill(n, 4)
        values = [job.readings[0].value_c for job in _drain(n)]
        # Job 0 was discarded; the queue holds 1, 2, 3.
        self.assertEqual(values, [1.0, 2.0, 3.0])

    def test_one_warning_per_drop(self):
        n = _notifier(queue_size=2)
        with self.assertLogs("lib.notifications.notifier", level="WARNING") as logs:
            self._fill(n, 5)
        drops = [line for line in logs.output if "discarded the oldest" in line]
        self.assertEqual(len(drops), 3)

    def test_queue_never_exceeds_capacity(self):
        # Bounded memory under a sustained outage is the point of the cap.
        n = _notifier(queue_size=4)
        self._fill(n, 50)
        self.assertEqual(len(_drain(n)), 4)


class ScopingTests(unittest.TestCase):
    def setUp(self):
        self.clock = 1000.0
        patcher = patch("lib.notifications.notifier.time.monotonic", lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_payload_carries_only_the_triggers_sensors(self):
        n = _notifier(trigger=GeneralTrigger(("CPU1 Temp",)))
        n.offer(_notification(("CPU1 Temp", 80.0), ("n8n GPU", 91.0)))
        self.assertEqual(_drain(n)[0].sensor_names, ("CPU1 Temp",))

    def test_unscoped_notifier_carries_everything(self):
        n = _notifier(trigger=GeneralTrigger(None))
        n.offer(_notification(("CPU1 Temp", 80.0), ("n8n GPU", 91.0)))
        self.assertEqual(len(_drain(n)[0].readings), 2)

    def test_missing_sensor_warns_once_not_every_poll(self):
        n = _notifier(trigger=GeneralTrigger(("CPU1 Temp", "Gone Temp")))
        with self.assertLogs("lib.notifications.notifier", level="WARNING") as logs:
            for _ in range(5):
                n.offer(_notification(("CPU1 Temp", 80.0)))
                self.clock += 60.0
        missing = [line for line in logs.output if "Gone Temp" in line]
        self.assertEqual(len(missing), 1)

    def test_remaining_data_is_still_delivered(self):
        n = _notifier(trigger=GeneralTrigger(("CPU1 Temp", "Gone Temp")))
        with self.assertLogs("lib.notifications.notifier", level="WARNING"):
            n.offer(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(_drain(n)[0].sensor_names, ("CPU1 Temp",))

    def test_reappearing_sensor_logs_recovery_and_rearms(self):
        n = _notifier(trigger=GeneralTrigger(("Flaky Temp",)))
        with self.assertLogs("lib.notifications.notifier", level="INFO") as logs:
            n.offer(_notification())                            # missing
            self.clock += 60.0
            n.offer(_notification(("Flaky Temp", 50.0)))        # back
            self.clock += 60.0
            n.offer(_notification())                            # missing again
        self.assertEqual(len([x for x in logs.output if "unavailable" in x]), 2)
        self.assertEqual(len([x for x in logs.output if "available again" in x]), 1)


class DeliveryTests(WorkerTestCase):
    """Real worker thread, waiting on Events rather than sleeping."""

    def _run_one(self, endpoint, **kwargs):
        n = _notifier(endpoint=endpoint, **kwargs)
        n.start()
        self.addCleanup(n.stop, 2.0)
        n.offer(_notification(("CPU1 Temp", 80.0)))
        return n

    def test_successful_delivery_reaches_the_endpoint(self):
        endpoint = FakeEndpoint()
        self._run_one(endpoint)
        self.assertTrue(endpoint.delivered.wait(_DEADLINE))
        self.assertEqual(len(endpoint.sent), 1)

    def test_transient_failure_is_retried_then_discarded(self):
        endpoint = FakeEndpoint(failures=[TransientEndpointError("boom")] * 3)
        n = _notifier(endpoint=endpoint, max_attempts=3)
        n.start()
        self.addCleanup(n.stop, 2.0)
        with self.assertLogs("lib.notifications.notifier", level="WARNING") as logs:
            n.offer(_notification(("CPU1 Temp", 80.0)))
            deadline = time.monotonic() + _DEADLINE
            while endpoint.calls < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(endpoint.calls, 3)
        self.assertTrue([x for x in logs.output if "failed after 3 attempt" in x])

    def test_permanent_failure_is_not_retried(self):
        # A rejected token fails identically every time; retrying would burn
        # the whole budget for nothing.
        endpoint = FakeEndpoint(failures=[PermanentEndpointError("401")] * 5)
        n = _notifier(endpoint=endpoint, max_attempts=3)
        n.start()
        self.addCleanup(n.stop, 2.0)
        with self.assertLogs("lib.notifications.notifier", level="WARNING") as logs:
            n.offer(_notification(("CPU1 Temp", 80.0)))
            deadline = time.monotonic() + _DEADLINE
            while endpoint.calls < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            time.sleep(0.05)
        self.assertEqual(endpoint.calls, 1)
        self.assertTrue([x for x in logs.output if "rejected" in x])

    def test_recovers_after_a_transient_failure(self):
        endpoint = FakeEndpoint(failures=[TransientEndpointError("blip")])
        self._run_one(endpoint, max_attempts=3)
        self.assertTrue(endpoint.delivered.wait(_DEADLINE))
        self.assertEqual(endpoint.calls, 2)

    def test_retry_after_drives_the_wait(self):
        waits = []
        real_wait = threading.Event.wait

        def record(self_event, timeout=None):
            if timeout is not None:
                waits.append(timeout)
            return real_wait(self_event, 0)  # do not actually wait

        endpoint = FakeEndpoint(
            failures=[TransientEndpointError("429", retry_after=7.0)] * 3
        )
        n = _notifier(endpoint=endpoint, max_attempts=3, retry_backoff_seconds=1.0)
        with patch.object(threading.Event, "wait", record):
            n.start()
            self.addCleanup(n.stop, 2.0)
            n.offer(_notification(("CPU1 Temp", 80.0)))
            deadline = time.monotonic() + _DEADLINE
            while endpoint.calls < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertIn(7.0, waits)

    def test_retry_after_is_capped(self):
        waits = []
        real_wait = threading.Event.wait

        def record(self_event, timeout=None):
            if timeout is not None:
                waits.append(timeout)
            return real_wait(self_event, 0)

        endpoint = FakeEndpoint(
            failures=[TransientEndpointError("429", retry_after=9999.0)] * 2
        )
        n = _notifier(endpoint=endpoint, max_attempts=2, retry_backoff_seconds=1.0)
        with patch.object(threading.Event, "wait", record):
            n.start()
            self.addCleanup(n.stop, 2.0)
            n.offer(_notification(("CPU1 Temp", 80.0)))
            deadline = time.monotonic() + _DEADLINE
            while endpoint.calls < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertTrue(waits)
        self.assertLessEqual(max(waits), 30.0)


class RobustnessTests(WorkerTestCase):
    def test_offer_never_raises_when_the_trigger_explodes(self):
        # offer() runs on the fan-control thread; nothing may propagate.
        n = _notifier(trigger=ExplodingTrigger())
        with self.assertLogs("lib.notifications.notifier", level="ERROR"):
            n.offer(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(len(_drain(n)), 0)

    def test_worker_survives_an_unexpected_endpoint_error(self):
        """Killing the network mid-delivery must leave the notifier working."""
        endpoint = FakeEndpoint(failures=[OSError("network went away")])
        n = _notifier(endpoint=endpoint, max_attempts=1)
        n.start()
        self.addCleanup(n.stop, 2.0)
        with self.assertLogs("lib.notifications.notifier", level="ERROR"):
            n.offer(_notification(("CPU1 Temp", 80.0)))
            deadline = time.monotonic() + _DEADLINE
            while endpoint.calls < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
        # The worker is still alive and takes the next job.
        n._next_due = 0.0
        n.offer(_notification(("CPU1 Temp", 81.0)))
        self.assertTrue(endpoint.delivered.wait(_DEADLINE))
        self.assertEqual(len(endpoint.sent), 1)


class ShutdownTests(WorkerTestCase):
    def test_stop_returns_promptly_against_a_wedged_endpoint(self):
        endpoint = FakeEndpoint(block=True)
        n = _notifier(endpoint=endpoint)
        n.start()
        self.addCleanup(endpoint.released.set)
        n.offer(_notification(("CPU1 Temp", 80.0)))
        self.assertTrue(endpoint.delivered.wait(_DEADLINE))

        started = time.monotonic()
        n.stop(timeout=0.2)
        elapsed = time.monotonic() - started
        # Bounded by the timeout, not by the endpoint.
        self.assertLess(elapsed, 1.0)
        endpoint.released.set()

    def test_wedged_worker_is_abandoned_with_a_warning(self):
        endpoint = FakeEndpoint(block=True)
        n = _notifier(endpoint=endpoint)
        n.start()
        self.addCleanup(endpoint.released.set)
        n.offer(_notification(("CPU1 Temp", 80.0)))
        self.assertTrue(endpoint.delivered.wait(_DEADLINE))
        with self.assertLogs("lib.notifications.notifier", level="WARNING") as logs:
            n.stop(timeout=0.1)
        self.assertTrue([x for x in logs.output if "abandoning" in x])

    def test_pending_jobs_are_discarded_not_drained(self):
        endpoint = FakeEndpoint(block=True)
        n = _notifier(endpoint=endpoint, queue_size=5)
        n.start()
        self.addCleanup(endpoint.released.set)
        n.offer(_notification(("CPU1 Temp", 80.0)))
        self.assertTrue(endpoint.delivered.wait(_DEADLINE))
        for value in (81.0, 82.0, 83.0):
            n._next_due = 0.0
            n.offer(_notification(("CPU1 Temp", value)))
        n.stop(timeout=0.1)
        endpoint.released.set()
        time.sleep(0.1)
        # Only the first, already in flight, ever reached the endpoint.
        self.assertLessEqual(len(endpoint.sent), 1)

    def test_stop_on_a_never_started_notifier_is_a_noop(self):
        _notifier().stop(timeout=0.1)

    def test_start_is_idempotent(self):
        n = _notifier()
        n.start()
        thread = n._thread
        n.start()
        self.addCleanup(n.stop, 2.0)
        self.assertIs(n._thread, thread)


class DryRunTests(WorkerTestCase):
    def test_schedules_and_queues_but_never_sends(self):
        endpoint = FakeEndpoint()
        n = _notifier(endpoint=endpoint, dry_run=True)
        n.start()
        self.addCleanup(n.stop, 2.0)
        with self.assertLogs("lib.notifications.notifier", level="INFO") as logs:
            n.offer(_notification(("CPU1 Temp", 80.0)))
            time.sleep(0.1)
        self.assertEqual(endpoint.calls, 0)
        self.assertTrue([x for x in logs.output if "[dry-run]" in x])

    def test_deliver_now_respects_dry_run(self):
        endpoint = FakeEndpoint()
        n = _notifier(endpoint=endpoint, dry_run=True)
        with self.assertLogs("lib.notifications.notifier", level="INFO"):
            n.deliver_now(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(endpoint.calls, 0)


class DeliverNowTests(unittest.TestCase):
    def test_sends_synchronously_without_a_worker(self):
        endpoint = FakeEndpoint()
        _notifier(endpoint=endpoint).deliver_now(_notification(("CPU1 Temp", 80.0)))
        self.assertEqual(len(endpoint.sent), 1)

    def test_applies_sensor_scoping(self):
        endpoint = FakeEndpoint()
        n = _notifier(endpoint=endpoint, trigger=GeneralTrigger(("CPU1 Temp",)))
        n.deliver_now(_notification(("CPU1 Temp", 80.0), ("n8n GPU", 91.0)))
        self.assertEqual(endpoint.sent[0].sensor_names, ("CPU1 Temp",))

    def test_propagates_the_failure(self):
        # --notify-test needs the real reason, not a swallowed warning.
        endpoint = FakeEndpoint(failures=[PermanentEndpointError("401 Unauthorized")])
        with self.assertRaises(PermanentEndpointError):
            _notifier(endpoint=endpoint).deliver_now(_notification())

    def test_makes_a_single_attempt(self):
        endpoint = FakeEndpoint(failures=[TransientEndpointError("boom")] * 5)
        with self.assertRaises(TransientEndpointError):
            _notifier(endpoint=endpoint, max_attempts=3).deliver_now(_notification())
        self.assertEqual(endpoint.calls, 1)


if __name__ == "__main__":
    unittest.main()
