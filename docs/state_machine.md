# State Machine

The daemon operates in one of five states.

```
STARTING
    │
    ▼
RUNNING ◄──────────────► FAILSAFE
    │                       ▲
    ├──────────────┐        │
    ▼              │        │
WARNING ◄──────────┼────────┘
    │              │
    ▼              │
EMERGENCY──────────┘
```

`EMERGENCY` takes precedence over `FAILSAFE`: the over-temperature checks run
first, so a visible sensor at the limit always gets full cooling.

## STARTING

Initialization.

## RUNNING

Normal operation.

## WARNING

Temperature approaching limits.

## EMERGENCY

Critical failure.

Actions:

- Maximum fan speed
- Restore iDRAC automatic control (if desired)
- Log critical event
- Shut down host if configured

## FAILSAFE

A known-good sensor has been unreadable for longer than
`safety.sensor_lost_grace_seconds`. The daemon can no longer see the whole
machine, so it stops driving the fans.

Actions:

- Hand fan control to iDRAC automatic mode, re-asserted every cycle
- Log a warning naming the lost sensor(s)
- Activate threshold notifiers that select the lost sensor(s)

Leaves `FAILSAFE` once every lost sensor reads again, returning to the curve
with no hysteresis carried over. Never requests a shutdown.

## Notifications

Notifier scheduling is independent of operating mode.

Entering `WARNING` or `EMERGENCY` does not bypass a notifier's configured `Interval`. Notifiers
fire only on their own trigger criteria and schedule, so no notifier depends on state outside its
own configuration. A threshold notifier configured below `safety.max_temperature` will observe
the same temperatures that drove the transition, but it does so through its own trigger, not
through the state machine.

No state transition can be delayed by notification activity, and the `EMERGENCY` actions above
are unchanged by the notification subsystem.