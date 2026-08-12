"""Owns the configured notifiers and feeds them the daemon's state.

This is the only component that sees both business-logic State and the
notification payload, which is why the conversion between them lives here.
Putting it in lib/notifications/ would force the I/O layer to import state.py
and invert the dependency direction.

It holds no endpoint-specific knowledge: it never learns how Discord
authenticates or what request Home Assistant expects. Those live behind the
endpoint interface, reached through the factory.

Threading: dispatch() and reload() both run on the daemon's main thread --
dispatch from the control loop, reload from between cycles -- so the notifier
set needs no lock. Everything handed to a worker is an immutable Notification.
"""

from __future__ import annotations

import time
from typing import Mapping, NamedTuple

from lib.factories.notifier_factory import create_notifier
from lib.models.notification import NotifierConfig, NotifierConfigError
from lib.notifications.notification import Notification, SensorReading
from lib.notifications.notifier import Notifier
from lib.state import State
from lib.utils.logging import get_logger

_log = get_logger(__name__)

_STOP_TIMEOUT_SECONDS = 2.0


class SelfTestResult(NamedTuple):
    """One notifier's outcome from a --notify-test run."""

    notifier: str
    ok: bool
    detail: str


class NotificationManager:
    """The set of live notifiers, and their lifecycle across reloads."""

    def __init__(
        self, configs: Mapping[str, NotifierConfig], dry_run: bool = False,
    ) -> None:
        self._dry_run = dry_run
        self._notifiers: dict[str, Notifier] = {}
        self._configs: dict[str, NotifierConfig] = {}
        built, _ = self._build(configs, reuse={})
        self._adopt(built, configs)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        for notifier in self._notifiers.values():
            notifier.start()

    def stop(self, timeout: float = _STOP_TIMEOUT_SECONDS) -> None:
        """Stop every worker, bounded by roughly one timeout in total.

        Every worker is signalled before any is joined, so they shut down in
        parallel. Signalling and joining one at a time would cost N times the
        timeout during teardown, delaying the point at which the daemon
        releases fan control.
        """
        self._retire(list(self._notifiers.values()), timeout)
        self._notifiers = {}
        self._configs = {}

    @staticmethod
    def _retire(notifiers: list[Notifier], timeout: float) -> None:
        """Stop these notifiers within `timeout` in total, not each.

        Every worker is signalled before any is joined, and the joins share one
        deadline. Signalling alone is not enough: a worker blocked inside an
        endpoint call cannot observe its stop event, so each join would
        otherwise wait out the full timeout and shutdown would cost N times as
        long. Once the deadline passes the remaining workers are abandoned --
        they are daemon threads, so they cannot hold the process open.
        """
        deadline = time.monotonic() + timeout
        for notifier in notifiers:
            notifier.request_stop()
        for notifier in notifiers:
            notifier.stop(max(0.0, deadline - time.monotonic()))

    # ------------------------------------------------------------------
    # Construction and reconciliation
    # ------------------------------------------------------------------
    def _build(
        self,
        configs: Mapping[str, NotifierConfig],
        reuse: Mapping[str, Notifier],
    ) -> tuple[dict[str, Notifier], list[str]]:
        """Assemble the notifier set a configuration mapping describes.

        Anything already running under an identical configuration is reused
        rather than rebuilt, which is what lets an untouched notifier keep its
        worker and its queued jobs across a reload. Nothing is started here.

        Returns the set plus the keys that were newly built, so the caller can
        start exactly those.
        """
        notifiers: dict[str, Notifier] = {}
        created: list[str] = []
        for key, config in configs.items():
            if not config.enabled:
                # Not built at all: no endpoint, no credential lookup, no
                # thread. Disabling is the same path as deleting the file.
                _log.debug("notifier %r is disabled; not starting it", key)
                continue
            existing = reuse.get(key)
            if existing is not None and self._configs.get(key) == config:
                notifiers[key] = existing
                continue
            try:
                notifiers[key] = create_notifier(config, dry_run=self._dry_run)
            except NotifierConfigError as exc:
                # One unusable notifier must not cost the others.
                _log.warning("notifier %r could not be created: %s", key, exc)
                continue
            created.append(key)
        return notifiers, created

    def _adopt(
        self, notifiers: dict[str, Notifier], configs: Mapping[str, NotifierConfig],
    ) -> None:
        self._notifiers = notifiers
        self._configs = {key: configs[key] for key in notifiers}

    def reload(self, configs: Mapping[str, NotifierConfig]) -> None:
        """Reconcile the live set against a freshly loaded configuration.

        The complete new set is built and validated before anything is swapped,
        so a failure partway through leaves the previous set untouched rather
        than half-replaced. Notifiers whose configuration did not change keep
        running, queue included; everything else is retired and its pending
        jobs discarded.
        """
        try:
            notifiers, created = self._build(configs, reuse=self._notifiers)
        except Exception:
            _log.exception("notifier reload failed; keeping the previous notifiers")
            return

        retained = set(notifiers.values())
        retired = [n for n in self._notifiers.values() if n not in retained]
        self._adopt(notifiers, configs)

        self._retire(retired, _STOP_TIMEOUT_SECONDS)
        for key in created:
            notifiers[key].start()

        _log.info(
            "notifiers reloaded: %d running, %d started, %d stopped",
            len(notifiers), len(created), len(retired),
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------
    def dispatch(self, state: State) -> None:
        """Offer the current state to every notifier. Never raises.

        Called from the control loop, so it performs no I/O: it builds one
        snapshot and hands it to each notifier, which decides for itself
        whether anything is due and queues a job if so.
        """
        if not self._notifiers:
            return
        try:
            notification = self.build_notification(state)
        except Exception:
            _log.exception("could not build a notification from the current state")
            return
        for key, notifier in self._notifiers.items():
            try:
                notifier.offer(notification)
            except Exception:
                # offer() already guards itself; this is the second layer, so
                # one bad notifier cannot cost the rest of the poll.
                _log.exception("notifier %r failed to accept a notification", key)

    @staticmethod
    def build_notification(state: State) -> Notification:
        """Convert mutable runtime state into a detached, immutable snapshot.

        Readings and alarms are sorted by name. `alarms` is a set, so its order
        is not stable between runs, and a sensor that fails and later recovers
        is re-inserted at the end of `temperatures` -- sorting keeps a
        notifier's output identical for identical inputs.
        """
        readings = tuple(
            SensorReading(name=name, value_c=reading.value, timestamp=reading.timestamp)
            for name, reading in sorted(state.temperatures.items())
        )
        result = state.last_command_result
        return Notification(
            timestamp=time.time(),
            readings=readings,
            fan_speed_percent=state.requested_fan_speed,
            operating_mode=state.mode.name,
            alarms=tuple(sorted(state.alarms)),
            last_command_ok=None if result is None else result.success,
        )

    # ------------------------------------------------------------------
    # --notify-test
    # ------------------------------------------------------------------
    def self_test(self) -> list[SelfTestResult]:
        """Deliver one synthetic notification per notifier, synchronously.

        Reports what happened rather than raising, so an operator sees every
        notifier's result in one run instead of only the first failure.
        """
        notification = _synthetic_notification()
        results: list[SelfTestResult] = []
        for key, notifier in self._notifiers.items():
            if self._dry_run:
                results.append(SelfTestResult(key, True, "skipped (dry run)"))
                continue
            try:
                notifier.deliver_now(notification)
            except Exception as exc:
                results.append(SelfTestResult(key, False, str(exc)))
            else:
                results.append(SelfTestResult(key, True, "delivered"))
        return results


def _synthetic_notification() -> Notification:
    """A recognisable payload for --notify-test.

    Named so that a message arriving in a real Discord channel is obviously a
    test rather than a genuine thermal event. Kept free of the word "fand" so
    a Home Assistant entity does not come out as sensor.fand_fand_self_test.
    """
    now = time.time()
    return Notification(
        timestamp=now,
        readings=(SensorReading(name="self test", value_c=42.0, timestamp=now),),
        fan_speed_percent=None,
        operating_mode="STARTING",
        alarms=(),
        last_command_ok=None,
    )
