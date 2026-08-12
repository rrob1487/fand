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

## Notification Configuration

Notifier definitions live in `config/notification/*.toml`, one notifier per
file. The daemon discovers them at startup and re-reads them on reload, the
same way VM configuration files are discovered.

`systemctl reload fand` takes effect within one `daemon.poll_interval`. The
signal handler only records the request; the reload itself runs between control
cycles, so nothing can interrupt the daemon mid-cycle.

Multiple files may configure the same endpoint type. Each file is independent.

The **file name is the notifier's identity**. `Name` is a human-readable label
for logs and diagnostics and does not need to be unique; the file path is what
the daemon uses to tell an added notifier from a modified one across a reload.

### Common Properties

| Key | Type | Required | Default | Notes |
|-----|------|----------|---------|-------|
| `Name` | string | yes | — | Human-readable label. Need not be unique. |
| `EndpointType` | string | yes | — | `discord` or `homeassistant`. |
| `Enabled` | bool | no | `true` | A disabled notifier stays configured but generates no jobs. |
| `Interval` | number | yes | — | Seconds between queued notifications while the trigger is satisfied. |
| `QueueSize` | int | yes | — | Maximum pending jobs, 1–10000. When full, the oldest is discarded. |
| `MaxAttempts` | int | no | `3` | Delivery attempts per job. |
| `RetryBackoff` | number | no | `1.0` | Seconds before the first retry, doubling per attempt, capped at 30. |

All durations are seconds, matching `daemon.poll_interval`.

Dispatch happens on the control loop, so an `Interval` shorter than
`daemon.poll_interval` is effectively clamped to the poll interval. The daemon
warns when a configuration does this.

### `[Trigger]`

| Key | Type | Required | Default | Notes |
|-----|------|----------|---------|-------|
| `Type` | string | yes | — | `threshold` or `general`. |
| `Temperature` | number | for `threshold` | — | Degrees Celsius, non-negative. Active while the hottest selected sensor is at or above this value. |
| `Sensors` | list of strings | no | all sensors | Scopes both the comparison and the payload. |

`Sensors` applies to both trigger types.

### `[Endpoint]`

Non-secret, endpoint-specific options.

| Key | Type | Applies to | Default | Notes |
|-----|------|------------|---------|-------|
| `Timeout` | number | all | `10.0` | Seconds per HTTP request. |
| `EntityPrefix` | string | `homeassistant` | `fand` | Entity id prefix, e.g. `sensor.fand_cpu1_temp`. |

### `[Credentials]`

Values are **environment variable names, never secret values**. The daemon
resolves them from the environment when it builds the endpoint. Secrets live in
`.env`, which is gitignored; `.env.example` names the variables without values.

A missing `.env`, or a variable it does not define, disables the notifiers that
need it and leaves the daemon running normally. The unit loads it with
`-EnvironmentFile=` for exactly that reason: notification credentials must never
be able to stop the machine being cooled.

| Endpoint type | Required | Optional |
|---------------|----------|----------|
| `discord` | `Token`, `Channel` | `Server` |
| `homeassistant` | `URL`, `Token` | — |

Discord's `Server` is optional: the API call addresses the channel directly, so
the value only identifies which guild a notifier targets in diagnostics.

A variable that is set but empty counts as unset — an empty token is a
misconfiguration, not a credential.

Keys the endpoint does not use are rejected, in both `[Credentials]` and
`[Endpoint]`. Those two tables are the only ones the schema does not own, so
this is the one place a typo in them can be caught.

### Sensor Names

`Sensors` entries must match the names the daemon uses internally:

- **IPMI sensors** use the BMC's own names, discovered at startup. Repeated
  names are disambiguated in encounter order: `"Temp"`, `"Temp #2"`.
- **GPU sensors** are named `"<vm name> GPU"`, one per configured VM.

Run the daemon with `-v` to see the discovered names.

A configured sensor that is not available is omitted and the remaining data is
still delivered.

### Error Handling

Notifier configuration errors are **not fatal**. An invalid file is logged and
skipped; the daemon, the fan-control subsystem, and every other notifier
continue running.

This differs deliberately from `config.toml` and `vms/*.toml`, where an invalid
file is fatal at startup. A missing fan curve means the machine cannot be
cooled. A missing notifier means a message is not sent.

**Unrecognized keys are rejected**, both at the top level and inside
`[Trigger]`. A misspelled optional key would otherwise fall back to its default
without warning, leaving a notifier that behaves differently from what its file
says — `MaxAttempt = 5` would quietly run with `MaxAttempts = 3`. Setting
`Temperature` on a `general` trigger is rejected for the same reason.

`[Endpoint]` and `[Credentials]` are exempt: their keys belong to the endpoint
implementation, so they are passed through without interpretation.

### Example

```toml
Name = "Discord Temperature Alerts"
EndpointType = "discord"
Enabled = true
Interval = 60
QueueSize = 10

[Trigger]
Type = "threshold"
Temperature = 80

[Endpoint]
Timeout = 10.0

[Credentials]
Token = "FAND_DISCORD_TOKEN"
Server = "FAND_DISCORD_SERVER"
Channel = "FAND_DISCORD_CHANNEL"
```