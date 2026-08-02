# Configuration

## Global Configuration

Contains daemon-wide settings.

Examples:

- Poll interval
- Logging level
- Fan curve
- Safety thresholds
- Watchdog options

## VM Configuration

Each VM defines:

- Name
- Guest Agent socket
- Sensor type
- Temperature limits
- GPU mappings

The daemon automatically discovers VM configuration files during startup.

## IPMI Sensor Discovery

IPMI temperature sensors (inlet, exhaust, per-CPU, ...) are not listed in
`config.toml`. Sensor count and naming vary by chassis, so the daemon
queries the BMC (`ipmitool sensor`) at startup and builds one temperature
sensor per sensor reported, the same way VM configuration files are
discovered automatically rather than hand-enumerated.