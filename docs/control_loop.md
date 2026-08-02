# Control Loop

Every polling interval:

1. Poll every configured sensor.
2. Update runtime state.
3. Evaluate safety conditions.
4. Compute desired fan speed.
5. Apply hysteresis.
6. Update iDRAC fan speed.
7. Notify systemd watchdog.
8. Sleep until next cycle.

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

Watchdog.notify()
```