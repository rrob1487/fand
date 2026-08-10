# State Machine

The daemon operates in one of four states.

```
STARTING
    │
    ▼
RUNNING
    │
    ├──────────────┐
    ▼              │
WARNING            │
    │              │
    ▼              │
EMERGENCY──────────┘
```

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

## Notifications

Notifier scheduling is independent of operating mode.

Entering `WARNING` or `EMERGENCY` does not bypass a notifier's configured `Interval`. Notifiers
fire only on their own trigger criteria and schedule, so no notifier depends on state outside its
own configuration. A threshold notifier configured below `safety.max_temperature` will observe
the same temperatures that drove the transition, but it does so through its own trigger, not
through the state machine.

No state transition can be delayed by notification activity, and the `EMERGENCY` actions above
are unchanged by the notification subsystem.