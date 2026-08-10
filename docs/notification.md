# Notification System

## Overview

The `fand` notification system provides a mechanism for sending fan and sensor information to external notification and monitoring services.

The notification system must be sufficiently abstracted that multiple external services can be supported without requiring the core fan-control daemon to understand the implementation details of each service.

Initial endpoint types are expected to include:

* Home Assistant
* Discord

Additional endpoint types should be able to be added in the future without requiring significant changes to the notification manager or fan-control logic.

The notification system is a **non-critical subsystem**. Failure of an external notification service must never prevent or interfere with the primary operation of the `fand` daemon.

---

# Design Goals

The notification system must:

1. Support multiple independent notification configurations.
2. Support multiple external endpoint types through a common abstraction.
3. Allow each notifier to independently define when it sends notifications.
4. Keep endpoint-specific implementation and authentication details isolated from the notification manager.
5. Load notifier configurations from TOML files.
6. Support configuration reload through the systemd reload mechanism.
7. Keep credentials outside of notifier configuration files.
8. Deliver notifications asynchronously so external services cannot block the fan-control loop.
9. Handle network and endpoint failures gracefully.
10. Log notification failures as warnings regardless of the configured logger verbosity.
11. Log successful notification delivery when debug logging is enabled.
12. Isolate queue capacity and failures between individual notification endpoints.
13. Allow additional endpoint types to be added without redesigning the notification framework.

---

# Architecture

The notification system should be divided conceptually into three primary components:

```text
Fan Daemon
    │
    ▼
Notification Manager
    │
    ├── Notifier A
    │      ├── Configuration
    │      ├── Queue
    │      └── Worker
    │
    ├── Notifier B
    │      ├── Configuration
    │      ├── Queue
    │      └── Worker
    │
    └── Notifier C
           ├── Configuration
           ├── Queue
           └── Worker
```

## Notification Manager

The notification manager is responsible for:

* Loading notifier configurations.
* Validating notifier configurations.
* Managing configured notifiers.
* Evaluating notifier trigger criteria.
* Creating notification jobs.
* Routing jobs to the appropriate notifier.
* Managing notifier lifecycle.
* Reloading notifier configurations.
* Ensuring notification processing does not block the main daemon.

The notification manager should not contain endpoint-specific logic.

For example, the notification manager should not need to know how Discord authentication works or what HTTP request Home Assistant requires.

---

## Notifier

A notifier represents one configured notification destination.

Each notifier has:

* A unique configuration.
* An endpoint type.
* Trigger criteria.
* A notification interval.
* A queue.
* A queue capacity.
* Endpoint-specific configuration and credentials.
* An endpoint implementation responsible for delivering notifications.

Every notifier operates independently of other notifiers.

A failure in one notifier must not prevent another notifier from operating.

For example, if Discord is unavailable:

```text
Discord
    Queue: Full
    Delivery: Failing

Home Assistant
    Queue: Normal
    Delivery: Successful
```

The Discord failure must not affect Home Assistant.

---

# Notification Endpoint Abstraction

The notification system must expose a generic notification interface to the rest of the application.

The fan-control code should provide notification data in a generic format rather than constructing endpoint-specific payloads.

Conceptually:

```text
Generic Notification
        │
        ├── Discord Endpoint
        │       └── Discord-specific payload
        │
        ├── Home Assistant Endpoint
        │       └── Home Assistant-specific payload
        │
        └── Future Endpoint
                └── Endpoint-specific payload
```

Each endpoint implementation is responsible for translating the generic notification into the format required by its external service.

Adding a new endpoint should not require the fan-control subsystem to understand the new service.

---

# Notification Configuration

Notifier configurations are stored as individual TOML files:

```text
/config/notification/*.toml
```

Each TOML file represents one notifier.

Multiple files may configure the same endpoint type.

For example:

```text
/config/notification/discord.toml
/config/notification/homeassistant.toml
/config/notification/homeassistant-secondary.toml
```

Each configuration is independent.

---

# Configuration Loading

Notifier configurations must be:

1. Loaded when `fand` starts.
2. Validated during loading.
3. Available to the notification manager after successful loading.
4. Reloadable through the systemd reload operation.

The systemd reload operation must cause notifier configurations to be re-read from disk.

A configuration reload must not require restarting the entire `fand` process.

---

## Configuration Reload Requirements

Configuration reloads must be handled safely.

A malformed or invalid configuration must not cause the main daemon to terminate.

If a configuration cannot be loaded or validated:

* The error must be logged.
* The affected notifier must not become active.
* Other valid notifiers must continue operating.
* The fan-control subsystem must continue operating normally.

The reload process must avoid leaving the notification system in a partially updated or inconsistent state.

---

# Notifier Configuration Structure

Each notifier configuration should contain the following common properties:

```toml
Name = "Discord Temperature Alerts"
EndpointType = "discord"
Enabled = true
Interval = 60
QueueSize = 10
MaxAttempts = 3
RetryBackoff = 1.0
```

`MaxAttempts` and `RetryBackoff` are optional and take the defaults shown.

A configuration also contains up to three nested tables:

| Table | Purpose |
|-------|---------|
| `[Trigger]` | When the notifier generates notifications. |
| `[Endpoint]` | Non-secret, endpoint-specific options. |
| `[Credentials]` | Environment-variable references for endpoint secrets. |

All durations in a notifier configuration — `Interval`, `RetryBackoff`, and any endpoint timeout — are expressed in **seconds**, matching `daemon.poll_interval` in `config.toml`.

## Name

`Name` is a human-readable name identifying the notifier.

It is intended primarily for:

* Logging.
* Diagnostics.
* Configuration identification.

It does not need to uniquely identify the notifier, although unique names are recommended.

Because `Name` is not required to be unique, it is not the notifier's identity. The **configuration file path** identifies a notifier. It is what the reload process uses to tell an added notifier from a modified one, and what diagnostics refer to when a `Name` is ambiguous.

Example:

```toml
Name = "Discord Temperature Alerts"
```

---

## EndpointType

`EndpointType` identifies the external service implementation used by the notifier.

Example:

```toml
EndpointType = "discord"
```

Initial endpoint types are expected to include:

* `discord`
* `homeassistant`

The endpoint type mechanism must be extensible so future services can be added.

---

## Enabled

`Enabled` controls whether the notifier is active.

Example:

```toml
Enabled = true
```

A disabled notifier may remain configured on disk but must not generate or deliver notification jobs.

This provides a mechanism for temporarily disabling a notifier without removing its configuration.

`Enabled` is optional. Omitting it means `true`.

---

## Interval

`Interval` specifies the amount of time between notification jobs being queued when the notifier's trigger criteria are satisfied.

Example:

```toml
Interval = 60
```

The interval is the **queue scheduling interval**, not the interval between successful deliveries.

For example, if a notifier has an interval of 60 seconds:

```text
12:00:00 → queue notification
12:01:00 → queue notification
12:02:00 → queue notification
```

Whether a notification is successfully delivered does not change the notifier's scheduling interval.

`Interval` is expressed in **seconds**, matching `daemon.poll_interval` in `config.toml`.

Notifications are dispatched from the fan-control loop, so a notifier can only be evaluated as often as the daemon polls.

An `Interval` shorter than `daemon.poll_interval` is therefore effectively clamped to the poll interval. Configuration loading must warn when a notifier requests an interval it cannot receive, rather than silently running on a slower schedule than its file describes.

---

## QueueSize

`QueueSize` specifies the maximum number of pending notification jobs that may exist for that notifier.

Example:

```toml
QueueSize = 10
```

Queue capacity is **per notifier**.

It is not a global notification-system limit.

Different endpoint types may require significantly different queue capacities.

For example:

```toml
QueueSize = 10
```

A notifier with a small queue may be appropriate for human-facing messaging services, while a notifier communicating sensor data to a monitoring system may benefit from a larger queue.

When a notifier's queue reaches capacity, the **oldest pending notification job must be discarded** to make room for the new job.

Queue overflow must not affect:

* Other notifier queues.
* Other notification workers.
* The fan-control loop.
* The operation of the main daemon.

Queue overflow should generate an appropriate warning log so that persistent delivery problems can be diagnosed.

---

## MaxAttempts

`MaxAttempts` specifies how many times a single notification job may be delivered before it is discarded.

Example:

```toml
MaxAttempts = 3
```

`MaxAttempts` is optional and defaults to `3`.

It bounds retry so that a permanently unavailable endpoint cannot consume worker resources indefinitely.

---

## RetryBackoff

`RetryBackoff` specifies the delay before the first retry, in seconds.

Example:

```toml
RetryBackoff = 1.0
```

The delay doubles on each subsequent attempt.

`RetryBackoff` is optional and defaults to `1.0`.

The computed delay is capped at **30 seconds**. This cap is a fixed safety bound rather than a configurable value: it exists so that a mistaken configuration cannot produce an unbounded wait inside a notification worker.

---

# Trigger Criteria

Each notifier independently defines the conditions under which it generates notification jobs.

Notifier criteria are **fully independent of all other notifiers**.

One notifier becoming active, inactive, or failing must not change the trigger state of another notifier.

The initial trigger types are:

* `threshold`
* `general`

The trigger type should be represented as a nested configuration:

```toml
[Trigger]
Type = "threshold"
```

or:

```toml
[Trigger]
Type = "general"
```

Additional trigger types may be added in the future.

## Common Trigger Properties

`Sensors` is common to every trigger type.

```toml
[Trigger]
Type = "threshold"
Sensors = ["CPU1 Temp", "CPU2 Temp"]
```

It selects which sensors the notifier considers. The selection scopes both the trigger's evaluation and the contents of the notification payload.

If `Sensors` is omitted, the notifier considers all available sensor data.

## Independence From Operating Mode

Trigger evaluation does not depend on the daemon's operating mode.

Entering `WARNING` or `EMERGENCY` does not bypass a notifier's `Interval` and does not queue an out-of-schedule notification.

A notifier that should report thermal emergencies expresses that through its own trigger criteria — for example, a `threshold` notifier configured below `safety.max_temperature` — rather than through daemon state.

This keeps every notifier self-contained, and keeps the notification subsystem out of the path of any state transition.

---

# Threshold Trigger

A threshold notifier generates notifications when sensor temperatures meet or exceed a configured temperature.

Example:

```toml
[Trigger]
Type = "threshold"
Temperature = 80
```

or, scoped to specific sensors:

```toml
[Trigger]
Type = "threshold"
Temperature = 80
Sensors = ["CPU1 Temp", "CPU2 Temp"]
```

The threshold comparison is:

```text
hottest selected sensor >= configured threshold
```

The relevant temperature is the **hottest reading among the notifier's selected sensors**. When `Sensors` is omitted, every available sensor is considered, so the notifier tracks the hottest sensor in the system — the same value the fan policy evaluates.

A notifier with no readings available for its selected sensors is not active. A notifier that cannot see a temperature cannot claim a threshold was crossed.

When the relevant sensor temperature reaches the threshold, the notifier becomes active.

The notifier should immediately queue a notification when the threshold is first reached.

While the threshold remains satisfied, additional notification jobs are queued according to the configured `Interval`.

When the temperature falls below the configured threshold, the notifier becomes inactive and stops scheduling additional notifications.

The threshold condition is therefore conceptually:

```text
Temperature < threshold
    │
    └── Not active

Temperature >= threshold
    │
    ├── Queue notification
    │
    ├── Wait Interval
    │
    ├── Queue notification
    │
    └── Continue until temperature < threshold
```

The notification subsystem must not continuously queue notifications faster than the configured interval.

---

# General Trigger

A general notifier generates notifications at the configured interval regardless of sensor temperature.

Example:

```toml
[Trigger]
Type = "general"
Sensors = ["CPU1 Temp", "CPU2 Temp"]
```

When `Sensors` is specified, only the listed sensors should be included in the notification payload.

If `Sensors` is omitted, the notifier must include all available sensor data.

For example:

```toml
[Trigger]
Type = "general"
```

means that all available sensor data is included on every notification cycle.

The general trigger does not depend on a temperature threshold.

---

# Sensor Selection

Sensor selection is evaluated independently for each notifier.

For example:

```text
Notifier A:
    Trigger = general
    Sensors = CPU1, CPU2

Notifier B:
    Trigger = general
    Sensors = GPU0, GPU1

Notifier C:
    Trigger = general
    Sensors = all
```

These configurations may operate simultaneously without affecting one another.

The notification system should use the generic sensor representation provided by the fan daemon rather than requiring endpoint-specific sensor handling.

Sensor selection applies to both trigger types. For a `general` notifier it determines the contents of the payload. For a `threshold` notifier it determines both the payload and the set of sensors compared against `Temperature`.

If a configured sensor is unavailable, the notification system should handle the missing sensor gracefully rather than terminating the notifier or daemon.

An unavailable sensor is omitted, and the remaining available data is still delivered.

A missing sensor is logged as a warning **once per sensor name per notifier**, not on every cycle. A misspelled entry in a `Sensors` list is a configuration mistake that persists, and repeating its warning every polling interval would bury genuine operational problems.

If none of a notifier's selected sensors are available, the notification is still delivered, carrying the fan state and system state it would otherwise have carried and no sensor readings.

---

# Notification Target Selection

The application is responsible for selecting notification targets.

The notification manager should not implement a global event-routing system in which events independently discover which notifiers should receive them.

Instead, application code determines which configured notification targets should be considered for a given notification operation.

Once a notifier is selected, that notifier independently evaluates its own trigger criteria.

This distinction allows notifier configurations to remain self-contained.

For example:

```text
Application
    │
    ├── Notifier A
    ├── Notifier B
    └── Notifier C

Each notifier:
    └── evaluates its own criteria independently
```

No notifier should depend on the trigger state of another notifier.

---

# Notification Payload

The notification system should operate on a generic notification representation.

The generic notification should contain the information necessary for endpoint implementations to construct their external payloads.

The notification representation should not contain endpoint-specific formatting.

Conceptually:

```text
Generic Notification
├── Timestamp
├── Sensor Data
├── Fan State
└── System State
```

The exact contents of the generic notification payload may evolve as the `fand` daemon gains additional information.

Endpoint implementations are responsible for converting the generic notification into the appropriate external representation.

For example:

```text
Generic Notification
        │
        ├── Discord
        │      └── Discord message
        │
        └── Home Assistant
               └── Home Assistant payload
```

The core fan-control logic must not need to know how individual endpoints format or transmit notification data.

---

# Endpoint-Specific Configuration

Different notification endpoints may require substantially different configuration and authentication information.

The common notifier configuration must therefore not assume that all endpoints require the same credentials.

Instead, each endpoint type defines its own credential/configuration schema.

For example, Discord may require:

* API token/key.
* Server/guild information.
* Channel information.

Home Assistant may require:

* Server URL.
* Bearer token.

These requirements must not be forced into a common credential model.

Conceptually:

```toml
[Credentials]
Token = "FAND_DISCORD_TOKEN"
Server = "FAND_DISCORD_SERVER"
Channel = "FAND_DISCORD_CHANNEL"
```

while another endpoint may define:

```toml
[Credentials]
URL = "FAND_HOMEASSISTANT_URL"
Token = "FAND_HOMEASSISTANT_TOKEN"
```

The names and contents of the `Credentials` fields are defined by the endpoint implementation.

The notification manager should treat endpoint credentials as opaque endpoint-specific configuration rather than attempting to interpret their meaning.

## Endpoint Options

Not all endpoint-specific configuration is secret.

Non-secret endpoint options belong in a separate `[Endpoint]` table:

```toml
[Endpoint]
Timeout = 10.0
```

or, for an endpoint that needs more:

```toml
[Endpoint]
Timeout = 10.0
EntityPrefix = "fand"
```

Separating the two tables keeps the rule about `[Credentials]` simple and absolute: every value in `[Credentials]` is an environment variable name, and nothing else belongs there.

Like `[Credentials]`, the contents of `[Endpoint]` are defined by the endpoint implementation, and the notification manager treats them as opaque.

`[Endpoint]` is optional. An endpoint that needs no options, or that is satisfied by its defaults, does not require the table.

---

# Environment Variables and Secrets

Secrets must not be stored directly in notification TOML files.

The TOML configuration should contain references to environment variables rather than the actual secret values.

For example:

```toml
[Credentials]
Token = "FAND_DISCORD_TOKEN"
```

with the actual secret stored in the environment file:

```text
FAND_DISCORD_TOKEN=<secret>
```

The environment file is:

```text
/.env
```

The notification system must resolve the configured environment-variable references when initializing an endpoint.

Credentials must not be written to logs.

Debug logging must never expose secret values.

The notification framework must allow different endpoint types to define different sets of required environment variables.

Resolved credentials are transmitted to the external endpoint on every delivery. The transport rules in [Security Requirements](#security-requirements) govern how.

---

# Asynchronous Notification Processing

Notification delivery must not execute synchronously as part of the main fan-control loop.

The fan daemon must be able to continue:

```text
Sensor acquisition
        ↓
Fan calculation
        ↓
Fan control
```

without waiting for:

* DNS resolution.
* Network connection establishment.
* TLS negotiation.
* External API responses.
* External API timeouts.
* Retries.
* Endpoint failures.

Notification jobs must therefore be queued and processed independently of the main fan-control execution path.

A notification operation should conceptually behave as:

```text
Fan Daemon
    │
    │ create notification job
    ▼
Notifier Queue
    │
    │ immediately return
    ▼
Fan Daemon continues
```

while a separate notification worker handles:

```text
Notifier Queue
    │
    ▼
Endpoint
    │
    ├── Success
    │
    └── Failure
```

The notification subsystem must never become a blocking dependency of fan control.

---

# Per-Notifier Queues

Every notifier must have its own notification queue.

There must not be a single global queue shared by all endpoints.

For example:

```text
Notification Manager
    │
    ├── Discord
    │     └── Queue (10)
    │
    └── Home Assistant
          └── Queue (100)
```

This allows queue capacity to reflect the characteristics of the destination.

Human-facing messaging services such as Discord may intentionally use a small queue because a large backlog of stale messages has limited value.

A monitoring-oriented endpoint such as Home Assistant may use a larger queue because individual sensor readings may have greater value as a sequence of observations.

A full queue in one notifier must never consume queue capacity belonging to another notifier.

---

# Queue Overflow

When a notifier queue reaches its configured capacity, the oldest pending notification job must be discarded when a new notification is queued.

Example:

```text
Queue capacity: 3

Existing:
    Job A
    Job B
    Job C

New:
    Job D

Result:
    Job B
    Job C
    Job D
```

This policy prioritizes current sensor information over stale notifications.

The dropped notification should not cause the daemon to fail.

Queue overflow should generate a warning log so that persistent queue saturation can be identified.

---

# Notification Delivery Failures

External notification systems are inherently unreliable from the perspective of the fan daemon.

Failures may include:

* Network connectivity problems.
* DNS failures.
* Connection failures.
* TLS failures.
* Endpoint timeouts.
* Authentication failures.
* Invalid endpoint configuration.
* HTTP/API errors.
* Rate limiting.
* Unexpected endpoint responses.

These failures must be handled gracefully.

A notification delivery failure must:

1. Not terminate the daemon.
2. Not block the fan-control loop.
3. Not prevent other queued notifications from being processed.
4. Not prevent other notifiers from operating.
5. Generate a warning log.

Notification failures are not considered fatal to `fand`.

## Failure Classification

Delivery failures are classified as **transient** or **permanent**, because the two deserve different handling.

| Classification | Failures | Handling |
|---|---|---|
| Transient | Connection, DNS, TLS, timeout, `5xx`, and `429` honouring `Retry-After` | Retried within `MaxAttempts` |
| Permanent | All other `4xx`, including authentication and authorisation failures | Discarded on the first attempt |

A rejected token fails identically on every attempt. Without this distinction, one mistyped credential would consume the full retry budget — and every backoff delay with it — for every job, indefinitely.

Both classifications generate a warning log, and neither deactivates the notifier.

---

# Retry Behavior

Transient notification delivery failures should be eligible for retry.

Retries must be handled by the notification subsystem rather than by the main fan-control loop.

Retries should use a bounded strategy so that a permanently unavailable endpoint does not consume worker resources indefinitely.

Only transient failures are retried. Permanent failures are discarded on the first attempt.

The retry strategy is exponential backoff:

* The first retry waits `RetryBackoff` seconds, which defaults to `1.0`.
* Each subsequent wait doubles.
* The wait is capped at 30 seconds.
* A `429` response's `Retry-After` value overrides the computed wait, still subject to the cap.
* A job is attempted at most `MaxAttempts` times, which defaults to `3`.

Backoff waits must be interruptible. A worker waiting to retry must abandon the attempt immediately when the daemon begins shutting down, rather than delaying teardown for the remainder of its delay.

After the retry limit is reached:

* The notification job should be discarded.
* A warning should be logged.
* The notifier should continue processing future jobs.

Retry behavior must not alter the notifier's configured scheduling interval.

For example, if a notification is scheduled every 60 seconds, a failed delivery and its retries must not change the future scheduling time of the notifier.

---

# Logging

The notification subsystem must integrate with the existing `fand` logging system.

## Normal Logging

Notification failures and other operational problems must be logged as warnings regardless of whether debug or verbose logging is enabled.

Examples include:

* Failed endpoint connection.
* Authentication failure.
* Invalid endpoint response.
* Queue overflow.
* Configuration errors.
* Failed notification delivery.

This ensures that notification problems are visible even during normal daemon operation.

---

## Debug Logging

When debug logging is enabled, every notification sent by the system should generate a debug log event.

A successful notification log should provide enough information to identify:

* Which notifier sent the notification.
* Which endpoint type was used.
* When it was sent.
* The result of the delivery.

Debug logging must not expose credentials, tokens, or other secrets.

The notification payload is not logged. A successful delivery record contains the notifier name, the endpoint type, the timestamp, and the result — enough to answer which notifier sent something, when, and whether it worked, without writing sensor and system data into the journal on every cycle.

---

# Configuration Errors

Invalid notifier configurations must not cause the fan daemon to terminate.

Examples include:

* Missing required fields.
* Invalid `EndpointType`.
* Invalid trigger type.
* Invalid trigger configuration.
* Invalid interval.
* Invalid queue size.
* Missing required credentials.
* Invalid endpoint-specific configuration.

Configuration errors should be associated with the affected notifier whenever possible.

A configuration error should:

1. Be logged clearly.
2. Prevent the invalid notifier from becoming active.
3. Not prevent valid notifiers from loading.
4. Not affect the fan-control subsystem.

---

# Configuration Reload

Notifier configurations must support runtime reload through the systemd reload mechanism.

Conceptually:

```text
systemctl reload fand
```

should cause the notification system to reload:

```text
/config/notification/*.toml
```

The reload should update the active notifier configuration without requiring a complete daemon restart.

Reload behavior must account for:

* Added notifier configurations.
* Removed notifier configurations.
* Modified notifier configurations.
* Disabled notifiers.
* Invalid configurations.

Notifiers are matched across a reload by **configuration file path**, not by `Name`, which is not required to be unique.

The reload process should safely transition from the previous configuration state to the new configuration state.

The new notifier set is built and validated first, and only then swapped in. A failure partway through construction therefore leaves the previous set fully intact rather than half-replaced.

Reload reconciles rather than rebuilds:

| Case | Action |
|---|---|
| Configuration unchanged | The running notifier is left alone. Its queue and worker survive. |
| Configuration added | The notifier is built and started. |
| Configuration removed | The notifier is stopped. |
| Configuration modified | The old notifier is stopped and a replacement is started. |
| Configuration invalid | The error is logged, the notifier is not started, and any previously running notifier for that file is stopped. |

Pending notification jobs belonging to a stopped notifier are discarded. Notifications are best effort, and a backlog of readings from before a configuration change has little value after it.

A configuration reload must not interrupt the fan-control subsystem.

---

# Notifier Lifecycle

A notifier may exist in several conceptual states:

```text
Configured
    │
    ▼
Validated
    │
    ▼
Active
    │
    ├── Trigger satisfied → Queue jobs
    │
    └── Trigger unsatisfied → No new jobs
```

A notifier may also enter an error condition:

```text
Active
    │
    ▼
Endpoint failure
    │
    ├── Retry
    │
    └── Eventually discard failed job
```

Endpoint failure does not inherently deactivate the notifier.

The notifier should continue attempting to process future notifications unless the configuration itself becomes invalid or the notifier is disabled.

## Stopping

A notifier is stopped when the daemon shuts down, when its configuration is removed, or when its configuration is modified.

Stopping discards any pending jobs.

The daemon must never delay shutdown waiting for a notification to be delivered. Releasing fan control back to iDRAC automatic mode always takes precedence, and happens before notifier shutdown is attempted.

---

# Operational Modes

Two `fand` invocation modes affect the notification subsystem.

## Dry Run

In `--dry-run`, notifiers evaluate their triggers, apply their interval scheduling, and log what they would have sent.

Nothing is delivered.

A dry run exists to show what the daemon would do without changing anything outside the process, and delivering a real message to Discord or Home Assistant is a change outside the process.

## Notification Test

`--notify-test` is a one-shot mode for validating notifier configuration and credentials.

It:

1. Loads notifier configurations.
2. Resolves credentials from the environment.
3. Delivers one synthetic notification per enabled notifier, synchronously.
4. Reports a per-notifier result.
5. Exits.

It does not enter the control loop, does not read sensors, and does not touch fan hardware.

This makes it possible to verify a token, URL, channel, or permission before relying on a notifier to report a real thermal event.

---

# Independence Between Notifiers

Each notifier must operate independently.

For example:

```text
Notifier A
    Endpoint: Discord
    Interval: 60
    QueueSize: 10
    Trigger: Threshold 80°C

Notifier B
    Endpoint: Home Assistant
    Interval: 30
    QueueSize: 100
    Trigger: General
```

Notifier A becoming active must not change the behavior of Notifier B.

Likewise:

* A full Discord queue must not affect Home Assistant.
* A Discord API failure must not affect Home Assistant.
* A Home Assistant authentication failure must not affect Discord.
* Different intervals must not affect one another.
* Different trigger states must not affect one another.

---

# Example Configurations

## Discord Threshold Notifications

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

This notifier:

* Uses Discord.
* Activates when the configured temperature reaches 80.
* Queues its first notification when the threshold is reached.
* Continues queuing notifications every 60 seconds while the threshold remains satisfied.
* Stops scheduling notifications when the temperature drops below 80.
* Maintains a maximum of 10 pending jobs.

---

## Home Assistant General Notifications

```toml
Name = "Home Assistant Sensors"
EndpointType = "homeassistant"
Enabled = true
Interval = 30
QueueSize = 100

[Trigger]
Type = "general"
Sensors = ["CPU1 Temp", "CPU2 Temp", "GPU0 Temp"]

[Endpoint]
Timeout = 10.0
EntityPrefix = "fand"

[Credentials]
URL = "FAND_HOMEASSISTANT_URL"
Token = "FAND_HOMEASSISTANT_TOKEN"
```

This notifier:

* Uses Home Assistant.
* Sends notifications every 30 seconds.
* Always includes the specified sensors.
* Does not depend on temperature thresholds.
* Maintains a maximum of 100 pending jobs.

---

## Home Assistant All-Sensor Notifications

```toml
Name = "Home Assistant All Sensors"
EndpointType = "homeassistant"
Enabled = true
Interval = 60
QueueSize = 100

[Trigger]
Type = "general"

[Endpoint]
Timeout = 10.0
EntityPrefix = "fand"

[Credentials]
URL = "FAND_HOMEASSISTANT_URL"
Token = "FAND_HOMEASSISTANT_TOKEN"
```

Because no `Sensors` list is specified, all available sensor data should be included in each notification.

---

# Extensibility

The notification architecture must allow additional endpoint types to be added without requiring fundamental changes to:

* Fan-control logic.
* Sensor acquisition.
* Notification scheduling.
* Trigger evaluation.
* Queue management.
* Configuration loading.
* The generic notification representation.

A future endpoint should primarily need to implement its endpoint-specific behavior and configuration requirements.

Potential future endpoint types may include services such as:

* Slack.
* Telegram.
* Email.
* ntfy.
* Gotify.
* Other monitoring or messaging systems.

These are examples of potential future integrations and are not requirements for the initial implementation.

---

# Non-Goals

The initial notification system is not intended to provide:

* Guaranteed delivery.
* Persistent notification queues across daemon restarts.
* A general-purpose message broker.
* Global event routing.
* Cross-notifier trigger dependencies.
* Interval bypass driven by the daemon's operating mode.
* Endpoint-specific logic in the fan-control subsystem.
* A universal credential schema shared by every endpoint.
* Notification delivery that can block fan control.

Notifications are considered **best effort**.

The primary responsibility of `fand` remains reliable fan and thermal management.

---

# Reliability Requirements

The notification subsystem must satisfy the following fundamental reliability requirements:

1. Notification failures must never stop fan control.
2. External network operations must never block the main fan-control loop.
3. Individual notifiers must be isolated from one another.
4. Individual notifier queues must be bounded.
5. Queue overflow must discard the oldest pending notification.
6. Configuration errors must not terminate the daemon.
7. Endpoint failures must not terminate the daemon.
8. Notification retries must be bounded.
9. Persistent endpoint failures must not cause unbounded resource consumption.
10. Notification configuration must be reloadable without restarting the daemon.

---

# Security Requirements

The notification system must:

1. Never store secrets directly in notifier TOML files.
2. Use environment variables for secret values.
3. Support endpoint-specific credential requirements.
4. Never expose credentials in normal or debug logs.
5. Never log notification payloads.
6. Treat external endpoint communication as untrusted network activity.
7. Fail safely when authentication or authorization fails.
8. Verify TLS certificates on every outbound request. Verification must never be disabled.
9. Warn once per endpoint URL when its scheme is not `https`, because credentials then travel in cleartext.
10. Reject a `[Credentials]` value that is not a valid environment variable name, naming the configuration key and never the value.

Non-HTTPS endpoints remain permitted rather than rejected. A Home Assistant instance on a trusted LAN is a legitimate deployment, and refusing it outright would push operators toward disabling certificate verification instead — a strictly worse outcome.

Requirement 10 exists because a value that is not an environment variable name is most likely a secret written into the wrong place. Rejecting it enforces requirement 1 rather than merely stating it.

---

# Design Summary

The notification system should provide a clean separation between **notification intent**, **notification scheduling**, and **endpoint delivery**.

The overall design is:

```text
                  Fan Daemon
                      │
                      ▼
             Notification Manager
                      │
             Application-selected
                   targets
                      │
          ┌───────────┼───────────┐
          ▼           ▼           ▼
       Notifier     Notifier    Notifier
          A           B           C
          │           │           │
       Criteria     Criteria    Criteria
          │           │           │
       Schedule     Schedule    Schedule
          │           │           │
       Queue        Queue       Queue
          │           │           │
       Worker       Worker      Worker
          │           │           │
       Endpoint     Endpoint    Endpoint
```

Each notifier is an independent unit with its own:

* Endpoint type.
* Configuration.
* Trigger criteria.
* Scheduling interval.
* Queue.
* Queue capacity.
* Credentials.
* Delivery worker.

The notification subsystem must remain entirely subordinate to the primary purpose of `fand`: maintaining appropriate system cooling.

If notification services are unavailable, misconfigured, overloaded, or completely offline, `fand` must continue controlling the fans normally.

