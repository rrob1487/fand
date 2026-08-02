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