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