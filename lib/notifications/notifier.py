"""One configured notification destination.

Owns the pieces that turn "should this fire?" into "this was delivered":
trigger evaluation, interval scheduling, a bounded queue, a worker thread, and
bounded retry.

Threading contract:

* `offer()` runs on the fan-control thread. It performs no I/O, takes no lock
  a worker holds, and never raises. Nothing it does may delay a control cycle
  or the watchdog.
* `_queue` has exactly ONE producer, `offer()`. The drop-oldest sequence below
  is only safe under that assumption: a second producer could interleave
  between the get and the put and lose a job. Adding one means revisiting it.
* Scheduling state (`_was_active`, `_next_due`, `_missing_sensors`) is touched
  only by the producer thread, so it needs no lock.
* Everything handed to the worker is an immutable `Notification`.
"""

from __future__ import annotations

import queue
import threading
import time

from lib.notifications.endpoint import (
    NotificationEndpoint,
    PermanentEndpointError,
    TransientEndpointError,
)
from lib.notifications.notification import Notification
from lib.notifications.trigger import Trigger
from lib.utils.logging import get_logger
from lib.utils.retry import retry

_log = get_logger(__name__)

# Bounds a retry wait however the configuration or a server's Retry-After is
# set. A fixed safety limit, not a tuning knob.
_MAX_RETRY_BACKOFF_SECONDS = 30.0

# How often an idle worker re-checks its stop event. Bounds how long stop()
# blocks when the queue is empty.
_QUEUE_POLL_SECONDS = 0.5


def _retry_after(exc: BaseException) -> float | None:
    """The server-supplied delay on a transient failure, if it gave one."""
    return getattr(exc, "retry_after", None)


class Notifier:
    """A trigger, a schedule, a queue, a worker, and an endpoint."""

    def __init__(
        self,
        name: str,
        endpoint: NotificationEndpoint,
        trigger: Trigger,
        interval_seconds: float,
        queue_size: int,
        max_attempts: int,
        retry_backoff_seconds: float,
        dry_run: bool = False,
    ) -> None:
        self._name = name
        self._endpoint = endpoint
        self._trigger = trigger
        self._interval = interval_seconds
        self._max_attempts = max_attempts
        self._dry_run = dry_run

        self._queue: queue.Queue[Notification] = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Producer-thread state.
        self._was_active = False
        self._next_due = 0.0
        self._missing_sensors: set[str] = set()

        # Named so retry's per-attempt warning says which notifier is failing
        # rather than reporting an anonymous bound method.
        def deliver(notification: Notification) -> None:
            self._endpoint.send(notification)

        deliver.__qualname__ = f"notifier {name!r} delivery"

        self._deliver = retry(
            exceptions=(TransientEndpointError,),
            attempts=max_attempts,
            backoff=retry_backoff_seconds,
            max_backoff=_MAX_RETRY_BACKOFF_SECONDS,
            cancel_event=self._stop,
            delay_override=_retry_after,
        )(deliver)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"notifier-{self._name}", daemon=True,
        )
        self._thread.start()

    def request_stop(self) -> None:
        """Ask the worker to stop, without waiting for it.

        Split from stop() so an owner of several notifiers can signal them all
        first and join them second. Joining one at a time would cost N times
        the timeout, and that time is spent during daemon teardown, which must
        not be delayed.
        """
        self._stop.set()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the worker and wait a bounded time for it.

        Pending jobs are discarded: notifications are best effort, and shutdown
        must never wait on an endpoint. The worker is a daemon thread, so one
        that ignores the deadline cannot hold the interpreter open and cannot
        stop the daemon from releasing fan control.
        """
        self.request_stop()
        thread, self._thread = self._thread, None
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            _log.warning(
                "notifier %r did not stop within %.1fs; abandoning its worker",
                self._name, timeout,
            )

    # ------------------------------------------------------------------
    # Producer side: fan-control thread
    # ------------------------------------------------------------------
    def offer(self, notification: Notification) -> None:
        """Evaluate this snapshot and queue a job if one is due.

        Never raises. A bug in a trigger or a payload must not reach the
        control loop.
        """
        try:
            self._schedule(notification)
        except Exception:
            _log.exception("notifier %r failed while scheduling", self._name)

    def _schedule(self, notification: Notification) -> None:
        if not self._trigger.is_active(notification):
            # Re-arm, so the next crossing fires immediately rather than
            # waiting out an interval that elapsed while inactive.
            self._was_active = False
            return

        now = time.monotonic()
        due = not self._was_active or now >= self._next_due
        self._was_active = True
        if not due:
            return

        self._enqueue(self._scope(notification))
        # Advanced only here. Delivery outcome, retries, and backoff never
        # touch it: Interval is the queueing interval, not the delivery one.
        self._next_due = now + self._interval

    def _scope(self, notification: Notification) -> Notification:
        names = self._trigger.sensor_names
        scoped = notification.with_sensors(names)
        if names is not None:
            self._track_missing(names, set(scoped.sensor_names))
        return scoped

    def _track_missing(self, requested: tuple[str, ...], available: set[str]) -> None:
        """Warn once per absent sensor, and note when one comes back.

        A misspelled name in a Sensors list persists forever, so warning every
        poll would bury real problems. Mirrors SensorManager's failed-sensor
        handling.
        """
        for name in requested:
            if name not in available:
                if name not in self._missing_sensors:
                    self._missing_sensors.add(name)
                    _log.warning(
                        "notifier %r: sensor %r is unavailable; "
                        "delivering the remaining data",
                        self._name, name,
                    )
            elif name in self._missing_sensors:
                self._missing_sensors.discard(name)
                _log.info("notifier %r: sensor %r is available again", self._name, name)

    def _enqueue(self, notification: Notification) -> None:
        try:
            self._queue.put_nowait(notification)
        except queue.Full:
            # Drop the oldest: current sensor data is worth more than a stale
            # backlog. Safe because offer() is the only producer.
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - single producer
                pass
            _log.warning(
                "notifier %r queue is full; discarded the oldest pending notification",
                self._name,
            )
            try:
                self._queue.put_nowait(notification)
            except queue.Full:  # pragma: no cover - single producer
                _log.warning("notifier %r could not queue a notification", self._name)
                return
        _log.debug("notifier %r queued a notification", self._name)

    # ------------------------------------------------------------------
    # Consumer side: worker thread
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                notification = self._queue.get(timeout=_QUEUE_POLL_SECONDS)
            except queue.Empty:
                continue
            try:
                self._deliver_job(notification)
            except Exception:
                # A worker must never die: an exception escaping a thread
                # cannot be caught by the daemon's main loop, and the notifier
                # would go silently deaf.
                _log.exception("notifier %r worker error", self._name)

    def _deliver_job(self, notification: Notification) -> None:
        if self._dry_run:
            _log.info(
                "[dry-run] notifier %r would deliver %d sensor reading(s) via %s",
                self._name, len(notification.readings), self._endpoint.endpoint_type,
            )
            return
        try:
            self._deliver(notification)
        except PermanentEndpointError as exc:
            # Not retried: it would fail identically on every attempt.
            _log.warning(
                "notifier %r: delivery rejected, discarding notification: %s",
                self._name, exc,
            )
        except TransientEndpointError as exc:
            _log.warning(
                "notifier %r: delivery failed after %d attempt(s), "
                "discarding notification: %s",
                self._name, self._max_attempts, exc,
            )
        else:
            _log.debug(
                "notifier %r delivered via %s (snapshot at %.0f): ok",
                self._name, self._endpoint.endpoint_type, notification.timestamp,
            )

    def deliver_now(self, notification: Notification) -> None:
        """Deliver once, synchronously, bypassing the queue and retry.

        For --notify-test: one attempt with the exception propagated, so an
        operator gets the real reason a notifier cannot reach its endpoint.
        Honours dry run, which promises nothing outside the process changes
        regardless of which path reaches the endpoint.
        """
        if self._dry_run:
            _log.info(
                "[dry-run] notifier %r would deliver via %s",
                self._name, self._endpoint.endpoint_type,
            )
            return
        self._endpoint.send(self._scope(notification))
