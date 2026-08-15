# Control Loop

Every polling interval:

1. Invalidate the cached BMC sensor table.
2. Re-scan the sensor set, if the re-scan interval has elapsed.
3. Poll every configured sensor.
4. Update runtime state.
5. Evaluate safety conditions.
6. Compute desired fan speed.
7. Apply hysteresis.
8. Update iDRAC fan speed.
9. Dispatch notifications.
10. Notify systemd watchdog.
11. Sleep until next cycle.

The controller should remain intentionally small.

Steps 1 and 2 belong to `SensorManager.poll()`, not to the controller.

**Step 1** means the BMC is queried once per cycle rather than once per sensor.
Every `IPMISensor` shares one parse, so all of a cycle's readings come from the
same instant and the cost of a cycle does not scale with the sensor count —
which matters because the watchdog is only kicked at step 10, after all of this.

**Step 2** exists because the sensor set is not fixed at startup. A BMC that is
still initialising — after an AC power loss, say — reports a short SDR, and a
set discovered once would stay short until the daemon was restarted. Re-scanning
picks up sensors that appear later and drops ones that vanish. A re-scan that
fails leaves the previous set alone: an empty set reads as "no temperature data",
which step 5 correctly escalates to an emergency.

```
Controller

↓

SensorManager.poll()

↓

State.update()

↓

Policy.calculate()

↓

IPMI.set_speed()

↓

NotificationManager.dispatch()

↓

Watchdog.notify()
```

## Dispatch

Dispatch comes after the fan speed has been applied, so the notification reports the state that
was actually acted upon — including the result of the fan command.

Dispatch performs no I/O and holds no long-lived lock. It evaluates each notifier's trigger,
checks its interval, and hands a job to a queue. Delivery happens on the notifier's own worker
thread.

It therefore cannot delay the watchdog ping or the next poll, no matter how slow or unreachable
an endpoint is.