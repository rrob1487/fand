# Control Loop

Every polling interval:

1. Poll every configured sensor.
2. Update runtime state.
3. Evaluate safety conditions.
4. Compute desired fan speed.
5. Apply hysteresis.
6. Update iDRAC fan speed.
7. Dispatch notifications.
8. Notify systemd watchdog.
9. Sleep until next cycle.

The controller should remain intentionally small.

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