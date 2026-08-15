# Notification Build Order

## Purpose

This document is the implementation roadmap for the `fand` notification subsystem defined in
`notification.md`.

It exists for the same reason as `build_order.md`: to define the order in which the subsystem
is implemented while preserving the architecture already established in:

- `architecture.md`
- `design_principles.md`
- `directory_map.md`
- `configuration.md`
- `control_loop.md`
- `state_machine.md`
- `build_order.md`
- `notification.md`

`build_order.md` remains the roadmap for the fan-control daemon itself. This document covers
only the notification subsystem and assumes every phase of `build_order.md` is complete.

Every file created by this document appears in `directory_map.md` — nothing is added beyond it
except the `__init__.py` package file noted in Phase 1, which is a mechanical necessity of the
directory structure `directory_map.md` specifies, not a new component.

---

## Build Philosophy

The notification subsystem follows the same bottom-up strategy as the rest of `fand`, and slots
into the existing layers rather than forming a parallel stack:

```
Application Layer          fand.py, daemon.py
        |
        v
Orchestration Layer        controller.py, managers/notification_manager.py
        |
        v
Business Logic Layer       policy.py, state.py
        |
        v
I/O Abstraction Layer      hardware/, notifications/
        |
        v
Infrastructure Layer       utils/
```

Allowed dependency flow for the new components:

```
daemon.py
    |
    v
controller.py
    |
    v
managers/notification_manager.py
    |
    +--> factories/notifier_factory.py
    |         |
    |         v
    |    models/notification.py
    |
    v
notifications/
    |
    v
utils/
```

Forbidden dependencies:

```
notifications/  → state.py
notifications/  → models/
notifications/  → managers/
notifications/  → controller.py
models/         → notifications/
policy.py       → notification_manager
```

`lib/notifications/` sits at the same level as `lib/hardware/`: it performs I/O and knows
nothing about daemon state. It therefore imports only from `lib/utils/` and from within its own
package. The conversion from mutable runtime `State` into the immutable notification payload
happens in `NotificationManager`, which is an orchestration component and is allowed to read
`State`.

### The overriding constraint

`notification.md` is unambiguous, and `CLAUDE.md`'s non-negotiable safety requirements agree:
**the notification subsystem is subordinate to fan control.** Every phase below is written so
that the answer to "what happens when this fails?" is always "the fans keep being controlled
correctly."

---

## Phase 0 — Documentation

**Purpose:** bring every existing document up to date with the notification subsystem *before*
any code is written, so that the documentation guides the implementation rather than trailing it.

**No `.py` file, configuration file, or `.gitignore` entry is touched in this phase.**

### `docs/directory_map.md`

**Must provide:**

- The updated tree:

```text
fand/
├── fand.py
├── .env
├── .env.example
├── config/
│   ├── config.toml.example
│   ├── vms/
│   │   └── vm.toml.example
│   └── notification/
│       ├── discord.toml.example
│       └── homeassistant.toml.example
├── lib/
│   ├── daemon.py
│   ├── controller.py
│   ├── policy.py
│   ├── state.py
│   ├── hardware/
│   │   ├── sensor.py
│   │   ├── ipmi.py
│   │   └── gpu.py
│   ├── notifications/
│   │   ├── notification.py
│   │   ├── endpoint.py
│   │   ├── discord.py
│   │   ├── homeassistant.py
│   │   ├── trigger.py
│   │   └── notifier.py
│   ├── managers/
│   │   ├── sensor_manager.py
│   │   ├── config_manager.py
│   │   ├── vm_manager.py
│   │   └── notification_manager.py
│   ├── models/
│   │   ├── config.py
│   │   ├── vm.py
│   │   └── notification.py
│   ├── factories/
│   │   ├── sensor_factory.py
│   │   └── notifier_factory.py
│   └── utils/
│       ├── qga.py
│       ├── retry.py
│       ├── http.py
│       └── logging.py
└── docs/
    ├── directory_map.md
    ├── build_order.md
    ├── notification_build_order.md
    └── architecture.md
```

- A new `### lib/notifications/` section describing the package as the notification I/O
  abstraction layer, with a per-file responsibility table.
- Rows added to the existing tables for `config/notification/`, `.env.example`,
  `lib/models/notification.py`, `lib/factories/notifier_factory.py`,
  `lib/managers/notification_manager.py`, and `lib/utils/http.py`.
- Rows added to the `docs/` table for `notification_build_order.md` and
  `diagrams/notification_flow.md`.

**Must NOT do:**

- Leave any file scheduled by this build order absent from the map.

### `docs/architecture.md`

**Must provide:**

- `NotificationManager` added to the Orchestration layer.
- Notification endpoints added as an I/O abstraction sibling of the Hardware Abstraction Layer,
  with a sentence stating that the upper layers never know how a notification is formatted or
  transmitted — the same guarantee already made for temperatures and fan speeds.
- HTTP transport added to the Infrastructure layer alongside Logging, Retry, and QGA.
- A statement that the notification subsystem is non-critical and that no layer above it may
  depend on its success.

### `docs/configuration.md`

**Must provide** — a new "Notification Configuration" section covering:

- Discovery of `config/notification/*.toml`, one notifier per file, multiple files per endpoint
  type permitted.
- The common schema and every key's type, default, and unit.
- `Interval` and all timeouts are expressed in **seconds**, matching `daemon.poll_interval`.
- The `[Trigger]`, `[Credentials]`, and `[Endpoint]` tables.
- `[Credentials]` values are **environment variable names**, never secret values.
- How sensor names are formed, so `Sensors` lists can be written correctly: IPMI sensors use the
  BMC's own names as discovered by `IPMI.temperature_sensor_names()` (including the `"Temp"`,
  `"Temp #2"` disambiguation, which is keyed on SDR sensor ID so a name always refers to the same
  physical sensor), and each VM contributes one sensor named `"<vm name> GPU"`.
- That a `Sensors` entry naming a sensor which is currently unreadable is **not** the same as one
  naming a sensor that does not exist. The former is reported as unavailable for as long as the BMC
  cannot read it and resumes on its own; the latter is a typo and warns once. Both leave the rest of
  the notification intact.
- An explicit note that **notifier configuration errors are non-fatal**: the affected notifier is
  logged and skipped while the daemon and all other notifiers continue. This deliberately differs
  from `config.toml` and `vms/*.toml`, where a bad file is fatal at startup, because a missing
  notifier does not endanger hardware and a missing fan curve does.

### `docs/control_loop.md`

**Must provide:**

- A new numbered step, "Dispatch notifications", inserted between "Update iDRAC fan speed" and
  "Notify systemd watchdog".
- The corresponding node in the inline flow diagram.
- A note that dispatch performs no I/O, acquires no long-held locks, and therefore cannot delay
  the watchdog ping or the next poll.

### `docs/state_machine.md`

**Must provide:**

- A note that notifier scheduling is independent of operating mode: entering `WARNING` or
  `EMERGENCY` does not bypass a notifier's `Interval`, and no state transition can be delayed by
  notification activity.
- A note that `EMERGENCY` actions are unchanged by this subsystem.

### `docs/diagrams/class_diagram.md`

**Must provide** — added relationships:

```mermaid
Controller --> NotificationManager
NotificationManager --> Notifier
NotifierFactory --> Notifier
Notifier --> Trigger
Notifier --> NotificationEndpoint
Trigger <|-- ThresholdTrigger
Trigger <|-- GeneralTrigger
NotificationEndpoint <|-- DiscordEndpoint
NotificationEndpoint <|-- HomeAssistantEndpoint
ConfigManager --> NotifierConfig
```

### `docs/diagrams/controll_flow.md`

**Must provide:**

- A `Dispatch Notifications` node between `Set Fan Speed` and `Kick Watchdog`.

> Note: the filename remains misspelled as `controll_flow.md`; `build_order.md`'s Documentation
> Note already records this. Do not rename it as part of this work.

### `docs/diagrams/startup.md`

**Must provide:**

- `NotificationManager` construction and worker-thread start, shown before `READY=1`, so the
  sequence records that notifiers are live by the time systemd considers the daemon ready.

### `docs/diagrams/notification_flow.md` *(new file)*

**Must provide** — a Mermaid flowchart of one notification's life:

```
Controller tick
    → NotificationManager.dispatch(state)
    → build immutable Notification snapshot (only if a notifier is due)
    → per notifier: Trigger.is_active?
        → no  → reset rising edge, done
        → yes → interval gate (rising edge fires immediately)
            → enqueue (drop oldest + warn if full)
    → worker thread: dequeue
        → Endpoint.send()
            → success   → debug log
            → transient → bounded backoff retry → exhausted → discard + warn
            → permanent → discard + warn
```

### `docs/notification.md`

**Must provide** — the revisions listed in [Specification Revisions](#specification-revisions)
at the end of this document. These resolve points `notification.md` explicitly leaves to the
implementation, plus the design decisions taken for this build.

### `docs/build_order.md`

**Must provide:**

- A single cross-reference line in the Purpose section pointing at `notification_build_order.md`
  as the roadmap for the notification subsystem.

**Must NOT do:**

- Renumber or restructure its existing phases.

### `README.md`

**Must provide:**

- Documentation table rows for `design_principles.md`, `notification.md`, and
  `notification_build_order.md` (the first two are currently missing).

### Phase Completion Criteria

- Every document above is updated and internally consistent.
- Every file scheduled by Phases 1–12 appears in `directory_map.md`.
- No `.py` file, configuration file, or `.gitignore` line has been modified.
- A developer could implement Phases 1–12 from the documentation alone.

---

## Phase 1 — Configuration Schema & Repository Plumbing

**Purpose:** establish the on-disk contract the rest of the subsystem reads, and make sure the
repository will actually track the example files.

### `.gitignore`

**Responsibility:** keep secrets and site-specific configuration out of git while tracking the
examples.

**Must provide** — a re-include block for the new configuration directory, mirroring the existing
`vms/` block:

```
!config/notification/
config/notification/*
!config/notification/*.example
```

**Why this is not optional:** the existing `config/*` rule excludes any new subdirectory of
`config/`, and `!config/*.example` does not reach into subdirectories. Without this block the
example notifier configurations are silently untracked, exactly as `config/vms/` would have been
without its own block.

### `.env.example`

**Responsibility:** checked-in template naming every environment variable the example notifier
configurations reference.

**Must provide:**

```
FAND_DISCORD_TOKEN=
FAND_DISCORD_SERVER=
FAND_DISCORD_CHANNEL=
FAND_HOMEASSISTANT_URL=
FAND_HOMEASSISTANT_TOKEN=
```

**Must NOT do:**

- Contain real values.
- Duplicate anything from `config.toml`.

`CLAUDE.md` requires a gitignored env file with a checked-in `.example`; the repository currently
has the former but not the latter.

### `config/notification/discord.toml.example`

**Must provide:**

```toml
Name = "Discord Temperature Alerts"
EndpointType = "discord"
Enabled = true
# Seconds between queued notifications while the trigger is satisfied.
Interval = 60
# Maximum pending jobs for this notifier. When full, the oldest is dropped.
QueueSize = 10
# Optional delivery policy; defaults shown.
MaxAttempts = 3
RetryBackoff = 1.0

[Trigger]
Type = "threshold"
Temperature = 80
# Optional. Omit to evaluate and report every available sensor.
# Sensors = ["CPU1 Temp", "n8n GPU"]

[Endpoint]
# Optional endpoint options; defaults shown.
Timeout = 10.0

# Values are environment variable NAMES, never secrets.
[Credentials]
Token = "FAND_DISCORD_TOKEN"
Server = "FAND_DISCORD_SERVER"
Channel = "FAND_DISCORD_CHANNEL"
```

### `config/notification/homeassistant.toml.example`

**Must provide:**

```toml
Name = "Home Assistant Sensors"
EndpointType = "homeassistant"
Enabled = true
Interval = 30
QueueSize = 100

[Trigger]
Type = "general"
# Omit to report every available sensor.
# Names come from the BMC ("CPU1 Temp") and from VMs ("<vm name> GPU").
Sensors = ["CPU1 Temp", "CPU2 Temp", "n8n GPU"]

[Endpoint]
Timeout = 10.0
# Prefix for the entities this notifier creates, e.g. sensor.fand_cpu1_temp
EntityPrefix = "fand"

[Credentials]
URL = "FAND_HOMEASSISTANT_URL"
Token = "FAND_HOMEASSISTANT_TOKEN"
```

### `lib/notifications/__init__.py`

**Responsibility:** enable Python package imports, as with every other `lib/` package.

### Phase Completion Criteria

- `git status` shows both example files as tracked.
- `.env.example` names every variable the examples reference and contains no values.
- The examples are byte-for-byte consistent with the schema documented in `configuration.md`.

---

## Phase 2 — Models

**Directory:** `lib/models/`

Models represent structured data. They perform no I/O and hold no secrets.

### `lib/models/notification.py`

**Responsibility:** typed representation of one notifier's configuration file.

**Depends on:** nothing (standard library only).

**Must provide** — frozen dataclasses with `from_dict()` classmethods, in the same shape as
`models/config.py` and `models/vm.py`:

```python
class NotifierConfigError(Exception): ...

@dataclass(frozen=True)
class TriggerConfig:
    sensors: tuple[str, ...] | None   # None = all available sensors

@dataclass(frozen=True)
class ThresholdTriggerConfig(TriggerConfig):
    temperature_c: float

@dataclass(frozen=True)
class GeneralTriggerConfig(TriggerConfig): ...

@dataclass(frozen=True)
class NotifierConfig:
    name: str
    endpoint_type: str
    enabled: bool
    interval_seconds: float
    queue_size: int
    max_attempts: int
    retry_backoff_seconds: float
    trigger: TriggerConfig
    credentials: Mapping[str, str]        # logical key -> env var NAME
    endpoint_options: Mapping[str, Any]   # non-secret endpoint options

    @classmethod
    def from_dict(cls, data: dict) -> "NotifierConfig": ...
```

**Validation** — every failure raises `NotifierConfigError` with a message naming the offending
key:

| Key | Rule |
|---|---|
| `Name` | required, non-empty string |
| `EndpointType` | required, non-empty string (membership is checked by the factory, which owns the registry) |
| `Enabled` | optional bool, default `true` |
| `Interval` | required, number `> 0` |
| `QueueSize` | required, int in `1 .. 10000` |
| `MaxAttempts` | optional int `>= 1`, default `3` |
| `RetryBackoff` | optional number `>= 0`, default `1.0` |
| `[Trigger].Type` | required, one of `threshold`, `general` |
| `[Trigger].Temperature` | required for `threshold`, number `>= 0` |
| `[Trigger].Sensors` | optional list of non-empty strings; omitted means all |
| `[Credentials]` | required table of string → string |
| `[Endpoint]` | optional table, passed through opaquely |
| Unknown keys | rejected at the top level and inside `[Trigger]`; `[Endpoint]` and `[Credentials]` stay opaque because the model does not own their schemas |

Rejecting unknown keys deviates from `models/config.py`, which pulls only the keys it recognizes.
It is correct here because notifier configuration is skipped rather than fatal: a mistyped
optional key would otherwise fall back to its default silently, leaving a notifier that behaves
differently from what its file says with nothing logged.

**Credential values must match `^[A-Za-z_][A-Za-z0-9_]*$`.** A value that fails this pattern is
almost certainly a secret pasted where a variable name belongs, so it is rejected — and the
rejection message names the key only, **never the value**.

**Must NOT do:**

- Read `os.environ`, open sockets, or touch the filesystem.
- Store a resolved secret. Holding only variable names makes leakage through `repr()`, a
  `dataclass` string conversion, or a traceback structurally impossible rather than a matter of
  discipline.
- Know which endpoint types exist.

### Phase Completion Criteria

- Every example configuration from `notification.md` parses into a `NotifierConfig`.
- Every invalid-configuration case listed in `notification.md`'s "Configuration Errors" section
  raises `NotifierConfigError` rather than a bare `KeyError` or `TypeError`.
- Two `NotifierConfig` values built from identical TOML compare equal — Phase 9's reload
  reconciliation depends on this.

---

## Phase 3 — Infrastructure Layer

**Directory:** `lib/utils/`

Infrastructure is stateless, reusable, and independent of application state.

### `lib/utils/http.py`

**Responsibility:** minimal JSON-over-HTTP transport.

**Depends on:** `urllib.request` from the standard library. **No new dependency** — `CLAUDE.md`
requires stdlib preference, and `urllib` covers the two POST-with-bearer-token endpoints this
subsystem needs.

**Must provide:**

```python
@dataclass(frozen=True)
class HTTPResponse:
    status: int
    body: str
    retry_after: float | None

class HTTPTransportError(Exception):
    """No response was obtained: DNS, connection, TLS, or timeout."""

def post_json(
    url: str,
    payload: Mapping[str, Any],
    headers: Mapping[str, str] | None = None,
    *,
    timeout: float,
) -> HTTPResponse: ...

def warn_if_insecure(url: str) -> None: ...
```

- **Every HTTP status returns normally**, including `4xx` and `5xx`. `HTTPTransportError` means
  the exchange never completed at all. The split is "did we reach the server", not "did we like
  the answer" — deciding that `429` is retryable and `401` is fatal is Phase 4's job, and a
  utility module must not encode that policy.
- A **mandatory, keyword-only** `timeout` on every request, so a caller cannot forget it. An
  endpoint that accepts a connection and never responds would otherwise park a worker thread
  forever, which is the one failure that turns a bounded queue into an unbounded backlog.
- Default TLS verification (never an unverified `SSLContext`).
- `Retry-After` parsed from the response headers when present, in both the delta-seconds and
  HTTP-date forms. A malformed value is ignored rather than fatal.
- **Redirects refused.** `urllib` follows them by default and re-sends request headers to the new
  location, so a `302` from an `https` endpoint to an `http` one would hand the `Authorization`
  header to the redirect target in cleartext. API endpoints have no legitimate reason to redirect;
  a `3xx` is returned to the caller as an ordinary response.
- Request construction happens **inside** the error handling. `urllib.request.Request` raises
  `ValueError` on an unusable URL, and an operator's typo in an endpoint URL must surface as a
  transport failure the caller already handles rather than as a stray exception.
- `warn_if_insecure(url)` warns when a URL would send credentials in cleartext. It is
  **stateless**: it keeps no record of what it has already warned about, because
  `design_principles.md` requires utility modules to hold no state. Callers control the frequency
  — Phase 4's endpoints call it once when they are constructed, which also re-announces after a
  reload rebuilds them. Non-HTTPS is permitted rather than rejected: a Home Assistant instance on
  a trusted LAN is a legitimate deployment, and refusing it would push operators toward disabling
  certificate verification instead, which is strictly worse.

**Must NOT do:**

- Know what a notification is.
- Retry. Retry policy belongs to the notifier, which is the component that knows how many
  attempts the configuration allows.
- Judge which statuses are errors.
- Log request bodies or header values. Error messages carry `scheme://host` only, since an
  endpoint URL can hold a token in its path.

### `lib/utils/retry.py` *(extension)*

**Responsibility:** unchanged — generic retry with exponential backoff.

**Must provide** — one backwards-compatible addition:

```python
def retry(
    *,
    exceptions=(Exception,),
    attempts: int = 3,
    backoff: float = 1.0,
    backoff_multiplier: float = 2.0,
    max_backoff: float | None = None,
    cancel_event: "threading.Event | None" = None,          # new
    delay_override: "Callable[[BaseException], float | None] | None" = None,   # new
): ...
```

`delay_override` lets the raised exception dictate its own wait, which is what Phase 6 needs to
honour an HTTP `429`'s `Retry-After`. It stays generic — the decorator asks a callable and learns
nothing about endpoints — and its result is still clamped by `max_backoff`. The exponential
sequence advances independently, so one server-supplied delay does not reset the ramp.

When `cancel_event` is supplied, backoff waits with `cancel_event.wait(delay)` instead of
`time.sleep(delay)`, and abandons the remaining attempts if the event is set, raising the last
exception as it would after exhausting them.

An already-set event is checked **before** the "retrying in Ns" log line, so the daemon never
announces a retry it is about to abandon.

Attempts themselves are never skipped — only the waits between them. A single attempt is already
bounded by its own timeout, and a notification worker checks its stop event before dequeuing.

**Failure mode this addresses:** a worker parked inside `time.sleep()` cannot observe a shutdown
request. On `SIGTERM` the daemon would then either wait out the full backoff or abandon the
thread mid-flight. Since a notification is best-effort, the correct behaviour is to abandon the
attempt immediately and let teardown proceed.

**Must NOT do:**

- Change behaviour for existing callers. `controller.py:119` and `sensor_manager.py:53` pass no
  `cancel_event` and must continue to use `time.sleep`.

### Phase Completion Criteria

- `post_json` returns an `HTTPResponse` for every status a reachable endpoint produces —
  `2xx`, `4xx`, `5xx`, and `3xx` alike — and raises `HTTPTransportError` only for connection
  refusal, timeout, TLS failure, and an unusable URL.
- `Retry-After` is parsed in both forms, and an unparsable value yields `None` rather than an
  error.
- The existing retry call sites are unmodified and behave identically.
- A `retry`-wrapped call with a set `cancel_event` abandons its remaining attempts without
  sleeping, and still raises the last exception.

---

## Phase 4 — Notification Payload & Endpoint Abstraction

**Directory:** `lib/notifications/`

This package is the I/O abstraction layer for notifications, structured exactly like
`lib/hardware/`: an abstract interface plus one module per implementation.

### `lib/notifications/notification.py`

**Responsibility:** the generic notification representation described in `notification.md`.

**Depends on:** nothing.

**Must provide:**

```python
@dataclass(frozen=True)
class SensorReading:
    name: str
    value_c: float
    timestamp: float

@dataclass(frozen=True)
class Notification:
    timestamp: float
    readings: tuple[SensorReading, ...]
    fan_speed_percent: float | None
    operating_mode: str
    alarms: tuple[str, ...]
    last_command_ok: bool | None

    def with_sensors(self, names: tuple[str, ...] | None) -> "Notification": ...

    @property
    def sensor_names(self) -> tuple[str, ...]: ...
    @property
    def hottest(self) -> SensorReading | None: ...
```

`with_sensors(None)` returns `self`; otherwise it returns a copy filtered to the named sensors,
preserving the configured order and silently omitting names that are absent. The **notifier**
reports the gap, by diffing its configured list against `sensor_names` — that keeps this a pure
data type rather than one that has to describe its own shortfalls.

`hottest` is a `max` over the readings, or `None` when there are none. It earns its place because
both the Discord headline and Phase 5's `ThresholdTrigger` need it, and neither should re-derive
it. It is a derived view of an immutable value object, not a decision — unlike `State`, which is
mutable and is deliberately kept free of evaluation.

**Must NOT do:**

- Contain endpoint-specific formatting.
- Hold a reference to anything mutable.

**Why immutability is a requirement, not a preference:** `State` is mutable and is written by the
controller thread on every cycle. Handing it — or any live view of it — to a worker thread would
be a data race, and the symptom would be a notification reporting a temperature that never
existed. `Notification` must be a fully detached snapshot of primitives.

### `lib/notifications/endpoint.py`

**Responsibility:** abstract notification endpoint interface.

**Depends on:** `notification.py`.

**Must provide:**

```python
class EndpointError(Exception): ...
class TransientEndpointError(EndpointError):
    retry_after: float | None
class PermanentEndpointError(EndpointError): ...

class NotificationEndpoint(ABC):
    @property
    @abstractmethod
    def endpoint_type(self) -> str: ...

    @abstractmethod
    def send(self, notification: Notification) -> None: ...

def raise_for_http_status(status: int, retry_after: float | None, context: str) -> None: ...
```

**Why the transient/permanent split exists:** a rejected bearer token fails identically on every
attempt. Without the distinction, a single typo in a token burns the full retry budget — including
its backoff sleeps — for every job, forever. Permanent failures are logged once per job and the
job is discarded.

`raise_for_http_status` holds the classification table both HTTP endpoints share, so it is
written once. It takes **primitives rather than an `HTTPResponse`**, so this module imports
nothing from `utils/http.py` — an endpoint that writes a file or a Unix socket fits the same
interface and simply never calls it.

**Must NOT do:**

- Know about configuration files, environment variables, or `State`.
- Import a transport. The interface is transport-agnostic.

### `lib/notifications/discord.py`

**Responsibility:** deliver a notification as a Discord message.

**Depends on:** `endpoint.py`, `notification.py`, `utils/http.py`.

**Must provide:**

```python
class DiscordEndpoint(NotificationEndpoint):
    def __init__(
        self,
        token: str,
        channel_id: str,
        server_id: str | None = None,
        base_url: str = "https://discord.com",
        timeout: float = 10.0,
    ) -> None: ...
```

- `POST {base_url}/api/v10/channels/{channel_id}/messages` with
  `Authorization: Bot <token>`, per `notification.md`'s credential schema.
- **A rich embed, colour-coded by operating mode** — green `RUNNING`, amber `WARNING`, red
  `EMERGENCY`, grey otherwise — with the hottest sensor as the headline, fan speed and mode as
  inline fields, the remaining readings in a code block, alarms when present, and an ISO-8601
  timestamp Discord renders in each viewer's local time. This is a human-facing service, so the
  message is a summary readable at a glance rather than a data dump.
- **The sensor block is truncated before sending.** Discord rejects a field value over 1024
  characters with a `400`, which the classifier treats as permanent — so an unbounded list would
  silently discard every notification from a machine with enough sensors. Excess rows collapse
  into a `... +N more` line.
- `base_url` is a constructor parameter rather than a constant: it gives tests a seam to point at
  a loopback server without patching module privates, and it lets `warn_if_insecure` apply to both
  endpoints uniformly. The default is https, and Phase 7 does not expose it in `[Endpoint]`.
- `warn_if_insecure(base_url)` is called at construction, which is what gives the Phase 3 check
  its "once per notifier, and again after a reload rebuilds it" cadence.
- Status classification:

| Response | Classification |
|---|---|
| `2xx` | success |
| `429` | transient, honouring `Retry-After` up to the retry cap |
| `5xx` | transient |
| other `4xx` | permanent |
| connection / DNS / TLS / timeout | transient |

`server_id` is accepted because `notification.md` specifies it, and is used for log identification
only — the REST call addresses the channel directly.

**Must NOT do:**

- Read `os.environ`. Credentials arrive as constructor arguments, already resolved.
- Log the token, the channel id, or the message body.

### `lib/notifications/homeassistant.py`

**Responsibility:** deliver a notification as Home Assistant entity states.

**Depends on:** `endpoint.py`, `notification.py`, `utils/http.py`.

**Must provide:**

```python
class HomeAssistantEndpoint(NotificationEndpoint):
    def __init__(
        self,
        base_url: str,
        token: str,
        entity_prefix: str = "fand",
        timeout: float = 10.0,
    ) -> None: ...
```

- `POST {base_url}/api/states/sensor.{prefix}_{slug}` per included sensor, with
  `Authorization: Bearer <token>`, sending `state` plus attributes
  (`unit_of_measurement: "°C"`, `device_class: "temperature"`, `friendly_name`).
- One additional entity for the requested fan speed and one for the operating mode, so the
  notifier reports the fan state and system state the generic payload carries.
- `slug` is the sensor name lowercased with every non-alphanumeric run collapsed to a single
  underscore, so `"CPU1 Temp"` → `sensor.fand_cpu1_temp` and `"Temp #2"` → `sensor.fand_temp_2`.
  `entity_prefix` is slugified the same way, falling back to `fand` if nothing survives.
- **Slug collisions are detected and logged.** `"CPU1 Temp"` and `"CPU1-Temp"` both reduce to
  `cpu1_temp`; without a check the second would overwrite the first in Home Assistant with nothing
  recorded. The first reading wins and the collision warns.
- **Every request is attempted**, rather than stopping at the first failure. `/api/states` is
  idempotent, so re-sending on a retry is harmless, and attempting all of them means one bad
  entity does not cost the rest of the data.
- The same status classification table as Discord. If any request within one job fails
  transiently, the whole job is transient; a permanent failure on any request fails the job
  permanently, since retrying a job containing one can never fully succeed.
- `warn_if_insecure(base_url)` at construction. This is the endpoint where it matters: the URL
  comes from operator configuration, unlike Discord's fixed API host.

**Must NOT do:**

- Read `os.environ`.
- Log the token or the resolved URL's credentials.

### Phase Completion Criteria

- Both endpoints implement `NotificationEndpoint` and are constructible from primitives alone.
- Neither module imports from `lib/models/`, `lib/managers/`, `lib/state.py`, or
  `lib/controller.py`.
- Adding a third endpoint requires no change to any file in this phase other than a new module.

---

## Phase 5 — Triggers

### `lib/notifications/trigger.py`

**Responsibility:** decide whether a notifier is currently active, and which sensors it cares
about.

**Depends on:** `notification.py`.

**Must provide:**

```python
class Trigger(ABC):
    def __init__(self, sensors: tuple[str, ...] | None) -> None: ...

    @property
    def sensor_names(self) -> tuple[str, ...] | None: ...

    @abstractmethod
    def is_active(self, notification: Notification) -> bool: ...


class ThresholdTrigger(Trigger):
    """Active while the hottest selected sensor is >= temperature_c."""

class GeneralTrigger(Trigger):
    """Always active."""
```

- `ThresholdTrigger.is_active` compares against the hottest reading among its selected sensors,
  matching how `policy.py:63` evaluates the hottest sensor for fan decisions. With no readings
  available it returns `False` — a notifier that cannot see a temperature cannot claim a
  threshold was crossed.
- `GeneralTrigger.is_active` returns `True` unconditionally, which is what makes the interval
  scheduling in Phase 6 a single code path for both trigger types.
- **`is_active` applies its own sensor selection**, calling `with_sensors(self.sensor_names)` on
  the snapshot it is given rather than requiring the caller to have scoped it first. Phase 6
  filters again to build the payload; that double filter is deliberate. An implicit
  "pass me a pre-scoped notification" precondition would fail *silently* if a future caller got
  it wrong — a threshold notifier firing on a sensor it was configured to ignore, with no error
  anywhere. `with_sensors(None)` returns the snapshot unchanged, so the unscoped case is free.

**Must NOT do:**

- Track time, schedule, or enqueue. Triggers answer "is the condition met right now?" and nothing
  else.
- Branch on trigger type anywhere outside this module.

### Phase Completion Criteria

- A threshold trigger with `Sensors` set ignores hotter sensors outside its list.
- A threshold trigger with `Sensors` omitted evaluates every reading.
- Adding a trigger type requires a new class here plus one factory entry — nothing else.

---

## Phase 6 — Notifier

### `lib/notifications/notifier.py`

**Responsibility:** one configured notification destination — trigger evaluation, interval
scheduling, bounded queue, worker thread, and bounded retry.

**Depends on:** `trigger.py`, `endpoint.py`, `notification.py`, `utils/retry.py`.

**Must provide:**

```python
class Notifier:
    def __init__(
        self,
        name: str,
        endpoint: NotificationEndpoint,
        trigger: Trigger,
        interval_seconds: float,
        queue_size: int,
        max_attempts: int,
        retry_backoff_seconds: float,
        dry_run: bool = False,
    ) -> None: ...

    def start(self) -> None: ...
    def request_stop(self) -> None: ...
    def stop(self, timeout: float) -> None: ...
    def offer(self, notification: Notification) -> None: ...
    def deliver_now(self, notification: Notification) -> None: ...   # --notify-test only
```

`request_stop()` signals without joining, so an owner of several notifiers can signal them all
before waiting on any. `stop()` is `request_stop()` plus a bounded join.

#### Scheduling — runs on the controller thread, performs no I/O

```
result = trigger.is_active(notification)

not active:
    was_active = False              # reset, so the next crossing fires immediately
    return

active and not was_active:          # rising edge
    enqueue; next_due = monotonic() + interval

active and was_active:
    if monotonic() >= next_due:
        enqueue; next_due = monotonic() + interval

was_active = True
```

This single path produces both behaviours `notification.md` specifies: a threshold notifier
queues immediately on first crossing and every `Interval` thereafter, and a general notifier —
being permanently active — queues immediately on the first cycle and every `Interval` thereafter.

`next_due` is advanced only here. Delivery outcome, retries, and backoff never touch it, which is
what `notification.md` means by "the queue scheduling interval, not the interval between
successful deliveries."

#### Queue

- `queue.Queue(maxsize=queue_size)`.
- On `queue.Full`: `get_nowait()` the oldest job, then `put_nowait()` the new one, then log a
  warning. This prioritises current sensor information over stale notifications, as specified.
- The drop-oldest sequence is safe because there is exactly one producer — the controller thread
  — and this assumption must be stated in the module docstring so a future change that adds a
  second producer is forced to revisit it.
- The queue is per notifier. A full Discord queue consumes nothing belonging to Home Assistant.

#### Worker

- `threading.Thread(target=..., daemon=True, name=f"notifier-{name}")`.
- Loop: `queue.get(timeout=_QUEUE_POLL_SECONDS)` inside a `while not stop_event.is_set()` guard,
  with `Empty` simply continuing. The 0.5 s poll bounds how long `stop()` blocks on an idle
  worker; it governs shutdown responsiveness only, so no behaviour depends on its value.
- **Shutdown uses the stop event, never a sentinel value in the queue.** Putting a sentinel into
  a full queue blocks — and a full queue is precisely the state a wedged endpoint produces, so
  the sentinel approach deadlocks exactly when shutdown matters most.
- `daemon=True` means a worker stuck in a socket call can never prevent interpreter exit, and
  therefore can never prevent the daemon from reaching the point where fan control is released.
- Every exception is caught inside the worker loop. A worker thread must never die, and an
  exception escaping a thread cannot be caught by the main loop's handler in `daemon.py:174`.

#### Delivery and retry

- Attempts bounded by `max_attempts`; backoff `retry_backoff_seconds` doubling per attempt,
  capped by a module constant (`_MAX_RETRY_BACKOFF_SECONDS = 30.0`) so a misconfigured value
  cannot produce an unbounded sleep.
- Only `TransientEndpointError` is retried; `TransientEndpointError.retry_after` overrides the
  computed backoff, still subject to the cap. This is `utils/retry.py`'s `delay_override` hook,
  wired as `lambda exc: exc.retry_after` — the notifier reuses the shared backoff, cap, and
  cancellation logic rather than reimplementing it.
- The retried callable's `__qualname__` is set to name the notifier, so retry's per-attempt
  warning says *which* notifier is failing instead of reporting an anonymous bound method.
- `PermanentEndpointError` discards the job on the first failure.
- Backoff waits are cancellable via the Phase 3 `cancel_event`, wired to the notifier's stop event.
- Attempts exhausted → discard the job, log a warning, continue with the next job. The notifier
  stays active; `notification.md` is explicit that endpoint failure does not deactivate a notifier.

#### Dry run

When `dry_run` is set, the notifier evaluates triggers, applies interval scheduling, and logs what
it would have sent — but never calls `endpoint.send()`. This mirrors `Controller._apply_fan_speed`
(`controller.py:76-80`): a dry run must not produce external side effects, and a real Discord
message is very much an external side effect.

`deliver_now` honours dry run too. `--dry-run` promises nothing outside the process changes, and
that promise should not depend on which path reaches the endpoint. Phase 11 reports such a
notifier as skipped rather than passed.

#### Logging

| Event | Level |
|---|---|
| delivery failure, retry exhaustion, permanent failure | `WARNING` |
| queue overflow / dropped job | `WARNING` |
| missing configured sensor (first occurrence per name) | `WARNING` |
| successful delivery | `DEBUG` |
| enqueued job | `DEBUG` |

Successful-delivery records carry notifier name, endpoint type, timestamp, and result — enough to
answer "which notifier sent what, when, and did it work?" without carrying the payload.
Credentials never appear at any level.

#### Missing sensors

A configured sensor absent from the snapshot is omitted and the remaining data is delivered, per
`notification.md`'s guidance to favour continuing delivery. The warning is emitted **once per
sensor name per notifier**, using the same seen-set pattern as
`SensorManager._failed_sensors` (`sensor_manager.py:39-47`) — otherwise a typo in a `Sensors`
list produces a warning on every poll interval forever.

A name that later reappears logs a recovery at INFO and is re-armed, matching how `SensorManager`
reports a sensor coming back. Without it, half of a VM restart goes unrecorded.

**Must NOT do:**

- Read configuration files or environment variables.
- Block the calling thread in `offer()` for any reason.
- Raise out of `offer()`.

### Phase Completion Criteria

- `offer()` returns in constant time and performs no I/O.
- A queue at capacity drops its oldest entry and logs exactly one warning per drop.
- A wedged endpoint produces bounded memory growth and a responsive `stop()`.
- Killing the network mid-delivery leaves the notifier processing subsequent jobs.

---

## Phase 7 — Factories

### `lib/factories/notifier_factory.py`

**Responsibility:** build a runtime `Notifier` from a validated `NotifierConfig`.

**Depends on:** `models/notification.py`, `lib/notifications/`.

**Must provide:**

```python
ENDPOINT_BUILDERS: dict[str, Callable[..., NotificationEndpoint]] = {
    "discord": _build_discord,
    "homeassistant": _build_homeassistant,
}

def create_endpoint(config: NotifierConfig) -> NotificationEndpoint: ...
def create_trigger(config: TriggerConfig) -> Trigger: ...
def create_notifier(config: NotifierConfig, dry_run: bool = False) -> Notifier: ...
```

- Resolves each `[Credentials]` entry through `os.environ` at construction time.
- Builds the `Trigger` from the trigger config.
- Builds the endpoint from resolved credentials plus `[Endpoint]` options.
- Returns a `Notifier` — not started; lifecycle belongs to the manager.

The registry maps to **builder functions rather than classes**, because each service needs
different credentials and options and so has a different constructor signature. A builder keeps
each endpoint's requirements in plain Python, so one with an unusual need does not force the
registry to grow a declarative mini-language. Trigger construction uses the same shape, keyed by
the *configuration class* rather than a type string, so `"threshold"` and `"general"` appear in
exactly one place — the model that parses them.

**Error handling** — a missing or empty environment variable, an unknown `EndpointType`, a missing
or unrecognised credential key, or an endpoint option of the wrong type raises
`NotifierConfigError` naming the endpoint type, the configuration key, and the **variable name**.
The variable's *value* is never included, because that message is going to a log.

- An environment variable that exists but is empty is treated as unset. An empty token is a
  misconfiguration, not a credential.
- **Unknown keys in `[Credentials]` and `[Endpoint]` are rejected.** The model passes both tables
  through opaquely because their schemas belong to the endpoint, which makes the factory the first
  thing able to catch `Timout = 5` or a credential key the endpoint never reads.
- Discord's `Server` is accepted but **not required**: the REST call addresses the channel
  directly, and the value only identifies the guild in diagnostics.

**Must NOT do:**

- Contain trigger or delivery logic.
- Start threads.

This mirrors `factories/sensor_factory.py` exactly. Adding an endpoint type requires: a new module
in `lib/notifications/`, a builder function, one `ENDPOINT_BUILDERS` entry, and a configuration
file — with no change to the manager, controller, daemon, or fan-control code.

### Phase Completion Criteria

- Adding an endpoint type touches exactly one existing line of code.
- A notifier referencing an unset environment variable fails construction with a message that
  names the variable and contains no secret.

---

## Phase 8 — Configuration Loading

### `lib/managers/config_manager.py` *(extension)*

**Responsibility:** unchanged — load configuration from the config directory.

**Must provide:**

```python
self.notifiers: dict[str, NotifierConfig]   # keyed by config file stem

def _discover_notifiers(self) -> dict[str, NotifierConfig]: ...
```

- Discovers `config/notification/*.toml` via `sorted(dir.glob("*.toml"))`, matching
  `_discover_vms` (`config_manager.py:48-60`).
- A missing `notification/` directory yields an empty mapping — notifications are optional.
- Called from `load()`, and therefore from `reload()`.

**Keyed by file stem, not by `Name`.** `notification.md` states that `Name` "does not need to
uniquely identify the notifier", so the file path is the only stable identity available for the
reload reconciliation in Phase 9. Two files may legitimately carry the same `Name`.

**Lenient loading — this is the one place where notification config deliberately diverges from
core config.** A notifier file that fails to parse or validate is logged as a warning and
skipped; `load()` still succeeds and every other notifier still loads. `config.toml` and
`vms/*.toml` keep raising `ConfigError`.

Three exception types are caught per file: `ConfigError` (unparsable TOML), `NotifierConfigError`
(failed validation), and `OSError` — one unreadable file must not stop the daemon starting.

**The `Interval` warning lives here.** `load()` reads `config.toml` before discovering notifiers,
making this the first point that can see both `daemon.poll_interval` and a notifier's `Interval`.
A notifier asking for less than the poll interval is warned about and still loaded: dispatch
happens on the control loop, so it simply runs at the poll cadence.

**`load()` is atomic.** It builds config, VMs, and notifiers into locals and assigns them at the
end, so a failure changes nothing. Previously it assigned as it went, so a failure in
`_discover_vms()` left the new `config` in place alongside the old `vms` — and
`Daemon.reload_config`'s "keeping previous configuration" (`daemon.py:110`) was not true. With a
third field that gets worse: a failed reload could advance the fan configuration while stranding
the notifier set.

**Why the asymmetry:** a missing fan curve means the daemon cannot cool the machine and must fail
loudly at startup. A missing notifier means a message does not get sent. `notification.md`
requires that a configuration error "must not prevent valid notifiers from loading" and "must not
affect the fan-control subsystem" — which a raised `ConfigError` propagating into
`Daemon.setup()` would violate on both counts.

**Must NOT do:**

- Resolve credentials or read `os.environ`.
- Construct endpoints, triggers, or notifiers.

### Phase Completion Criteria

- One corrupt `.toml` in `config/notification/` produces one warning and zero effect on the
  daemon or the other notifiers.
- An absent `config/notification/` directory is not an error.
- `reload()` re-reads the directory from disk.

---

## Phase 9 — Notification Manager

### `lib/managers/notification_manager.py`

**Responsibility:** own the set of notifiers, convert runtime `State` into the generic
notification, route it to notifiers, and manage notifier lifecycle across reloads.

**Depends on:** `factories/notifier_factory.py`, `models/notification.py`,
`lib/notifications/notification.py`, `lib/state.py`.

**Must provide:**

```python
class NotificationManager:
    def __init__(self, configs: Mapping[str, NotifierConfig], dry_run: bool = False) -> None: ...
    def start(self) -> None: ...
    def stop(self, timeout: float = 2.0) -> None: ...
    def dispatch(self, state: State) -> None: ...
    def reload(self, configs: Mapping[str, NotifierConfig]) -> None: ...
    def self_test(self) -> list[tuple[str, bool, str]]: ...
```

#### `dispatch(state)`

1. Skip entirely if no notifier is enabled — the common case when nothing is configured.
2. Build **one** immutable `Notification` snapshot from `State`, reading `temperatures`,
   `requested_fan_speed`, `mode`, `alarms`, and `last_command_result`.
3. Offer that snapshot to each notifier; each filters it to its own `Sensors` selection.
4. Wrap each notifier's `offer()` individually, so one misbehaving notifier cannot affect
   another — the same isolation `SensorManager.poll` gives sensors (`sensor_manager.py:35-50`).

`dispatch` returns immediately when no notifier is active, so a daemon with nothing configured
pays nothing. It cannot skip the snapshot any more cleverly than that: whether a notifier is due
depends on its trigger, and evaluating a trigger requires the snapshot the check would be trying
to avoid building.

**Readings and alarms are sorted by name.** `state.alarms` is a `set`, whose iteration order is
not stable between runs, and a sensor that fails and later recovers is re-inserted at the end of
`state.temperatures`, so insertion order drifts. Sorting keeps identical inputs producing
identical output, per `CLAUDE.md`'s predictability requirement. Scoped notifiers are unaffected —
`with_sensors` already reorders to the configured order.

**Disabled notifiers are not constructed at all**: no endpoint, no credential resolution, no
thread. `notification.md` requires that a disabled notifier "must not generate or deliver
notification jobs", and never building it is the most direct reading. Setting `Enabled = false`
therefore retires a notifier through exactly the same path as deleting its file.

`State` → `Notification` conversion lives here, not in `lib/notifications/`, because it is the
only point in the design that legitimately sees both. Putting it lower would force the I/O layer
to import business-logic state and invert the dependency direction.

#### `reload(configs)` — reconcile, do not rebuild

| Case | Action |
|---|---|
| config unchanged (frozen dataclass equality) | leave the running notifier alone — worker and queued jobs survive |
| config added | build, start |
| config removed | stop, drop |
| config modified | stop old, build and start replacement |
| config invalid | log, skip; the previously running notifier for that file is stopped and not replaced |

The complete new notifier set is built and validated **first**, and only then swapped in; the old
notifiers being retired are stopped afterwards. This is what `notification.md` means by "must
avoid leaving the notification system in a partially updated or inconsistent state" — a failure
halfway through construction leaves the previous set fully intact.

Pending jobs belonging to a stopped notifier are discarded. `notification.md`'s non-goals
explicitly exclude guaranteed delivery and persistent queues.

#### `stop(timeout)`

Signals every stop event, then joins each worker against a **single shared deadline**, so the
whole shutdown is bounded by one `timeout` regardless of how many notifiers there are. Workers
that do not return by the deadline are abandoned rather than waited on — they are daemon threads
and cannot hold the process open.

Signalling first is necessary but not sufficient: a worker blocked *inside* an endpoint call
cannot observe its stop event, so per-notifier timeouts would still cost `N x timeout`. That time
is spent in `Daemon.teardown()`, delaying the point at which fan control returns to iDRAC, which
is why the deadline is shared rather than per-worker.

**Must NOT do:**

- Contain endpoint-specific logic. The manager must never need to know how Discord authenticates
  or what HTTP request Home Assistant expects.
- Raise into the control loop. `dispatch()` returns cleanly under every failure.
- Perform network I/O on the calling thread (except in `self_test()`, which is not called while
  the daemon is running).

### Phase Completion Criteria

- `dispatch()` performs no I/O and never raises.
- `systemctl reload` with one file added, one removed, one edited, and one untouched produces
  exactly one start, one stop, one restart, and zero disruption to the fourth.
- Stopping with a hung endpoint returns within the timeout.

---

## Phase 10 — Controller Integration

### `lib/controller.py` *(extension)*

**Must provide:**

- An optional `notification_manager: NotificationManager | None = None` constructor parameter,
  defaulting to `None` so the controller remains constructible and testable without the
  subsystem.
- A dispatch step at the end of `run_cycle()`, after `_apply_fan_speed()`:

```
1. Poll sensors
2. Update state
3. Evaluate safety (via Policy)
4. Compute desired fan speed (via Policy)
5. Apply hysteresis
6. Set fan speed
7. Dispatch notifications        <- new
8. (Daemon) Kick watchdog
```

- The call wrapped in `try/except Exception` that logs a warning and continues.

**Why dispatch comes after the fan speed is applied:** the notification then reports the state
that was actually acted upon, including `last_command_result`. It also guarantees that no
notification work can sit between a decision and its execution.

**And after the shutdown request, not before it.** Dispatch is the last statement in `run_cycle`,
so on an emergency cycle nothing at all sits between the decision and `systemctl poweroff`. The
alternative — dispatching before the shutdown branch — would only delay it by a queue write, but
"only a little" is a worse invariant than "not at all" in a safety loop, and a future reader
should not have to weigh it.

Nothing is lost by the ordering: `_shutdown_host()` does not mutate `state`, so the snapshot is
identical either way, and `systemctl poweroff` returns as soon as systemd accepts the job, so a
notification queued just after it has the same shutdown window as one queued just before.

**Why it is still wrapped despite the manager's own guards:** `CLAUDE.md` requires that one
misbehaving component never blocks the poll loop or delays a critical-temp response elsewhere.
Two independent guards is the correct amount for a non-critical subsystem embedded in a safety
loop.

**Must NOT do:**

- Decide what to notify about. The controller hands over state; notifiers decide.
- Let a shutdown request be delayed by dispatch — `decision.shutdown_requested` handling stays
  where it is.

### Phase Completion Criteria

- A `NotificationManager` that raises on every call leaves the control loop's behaviour
  bit-for-bit unchanged.
- `Controller` with `notification_manager=None` behaves exactly as before.

---

## Phase 11 — Application Layer Integration

**This is the highest-risk phase.** It touches `daemon.py`, which owns startup, signal handling,
watchdog, and the teardown path that releases fan control.

### `lib/daemon.py`

#### Notification manager lifecycle

- Constructed **once** in `setup()`, after `ConfigManager.load()`, and started there.
- Passed into `_build_controller()` but **not owned by it**.

**Failure mode this avoids:** `_build_controller()` discards and recreates the `Controller` on
every reload (`daemon.py:108`). If the notification manager were created there, every `SIGHUP`
would tear down and respawn every worker thread and discard every queued job — including on a
reload that changed nothing about notifications.

#### `SIGHUP` must stop doing work inside the signal handler

`_handle_reload_signal` currently calls `reload_config()` directly from the signal handler
(`daemon.py:97-99`).

**Failure mode:** Python signal handlers run on the main thread between bytecodes and can
interrupt anything — including a `logging` call that holds the module-level lock. `reload_config()`
logs. If the signal lands inside a logging call, the handler deadlocks the daemon, the watchdog
stops being kicked, and systemd restarts the process. Adding `Thread.start()` and `Thread.join()`
to that path widens the hazard substantially, because both take locks of their own.

**Change:** `_handle_reload_signal` sets `self._reload_requested = True` and returns. `run()`
performs the reload between cycles, in the same place it already checks `_shutdown_requested`.

This is a latent defect today, not one the notification work introduces — but the notification
work is what makes it likely enough to matter, so it is fixed here.

**`SIGTERM`/`SIGINT` has the identical defect and is fixed in the same pass.**
`_handle_shutdown_signal` also logs. If that lands while the logging lock is held, the daemon
never observes the shutdown request at all: systemd escalates to `SIGKILL` after
`TimeoutStopSec`, `teardown()` never runs, and fan control is never released cleanly — only
`ExecStopPost` saves the machine. Both handlers are reduced to setting a flag and returning, and
both log lines move into `run()`, where the flags are read.

Phase 11 is what makes both likely: notifier workers log on every delivery, retry, and drop, so
the logging lock is now held a large fraction of the time, on threads the main thread does not
coordinate with.

**Accepted trade-off:** reload is no longer immediate. `run()` checks the flag between cycles, so
`systemctl reload` takes effect within one `poll_interval` rather than instantly. Correctness over
latency, and reload is not latency-sensitive.

#### `reload_config()`

After `self._config_manager.reload()` succeeds, call
`self._notification_manager.reload(self._config_manager.notifiers)` inside its own
`try/except Exception`. A notification reload failure logs and keeps the previous notifier set;
it never prevents the fan-control configuration reload from taking effect.

#### `teardown()` — ordering is a safety requirement

```python
def teardown(self) -> None:
    try:
        if self._controller is not None:
            self._controller.release_fan_control()   # FIRST, always
    finally:
        if self._notification_manager is not None:
            try:
                self._notification_manager.stop(timeout=...)
            except Exception:
                self.log.exception("notification shutdown failed")
    self.log.info("Shut down cleanly")
```

**Failure mode this addresses:** `CLAUDE.md`'s third non-negotiable requirement is that daemon
exit always releases fan control back to iDRAC. If notification shutdown ran first and blocked or
raised, the fans would be left pinned under manual control with no daemon driving them. Releasing
fan control first, in its own `try`, makes notification shutdown incapable of interfering.
`fand.service`'s `ExecStopPost=/usr/bin/ipmitool raw 0x30 0x30 0x01 0x01` remains the final
backstop.

Worker threads are `daemon=True` and are joined with a bounded timeout, so a hung endpoint cannot
extend shutdown past `TimeoutStopSec`.

#### `--notify-test` support

```python
def run_notify_test(self) -> int: ...
```

Loads configuration and the notification manager **without initialising IPMI or sensors**,
delivers one synthetic notification per enabled notifier synchronously via
`NotificationManager.self_test()`, logs one line per notifier, and returns `0` when all succeed
and `1` otherwise. It never enters the control loop and never touches fan hardware.

Exit codes distinguish three cases, because an operator scripting this needs the code to mean
something:

| Situation | Result |
|---|---|
| No enabled notifiers configured | warning, exit `0` — nothing failed |
| A notifier could not be built (missing credential, unknown endpoint type) | counted as a failure, exit `1` |
| Every notifier delivered | exit `0` |

A notifier that could not be constructed is deliberately **not** silently absent from the report.
The manager already logs why it was skipped, but reporting a pass for a notifier that never ran
would defeat the point of asking for a test.

### `fand.py`

**Must provide:**

- A `--notify-test` flag in the same style as `--dry-run`, dispatching to
  `Daemon.run_notify_test()` instead of `Daemon.run()`.

**Must NOT do:**

- Gain any notification logic of its own. `fand.py` parses arguments, configures logging, loads
  the environment, and creates the daemon — nothing more.

Credentials reach the process through the existing `.env` mechanism: `fand.py:20-33` loads it for
manual runs, and `fand.service`'s `EnvironmentFile=/opt/fand/.env` loads it under systemd. No new
mechanism is required.

### Phase Completion Criteria

- `SIGHUP` reload runs on the main loop, not in the handler.
- `SIGTERM` with a wedged notification endpoint still releases fan control, and does so before
  worker shutdown is attempted.
- `--notify-test` runs without touching IPMI.
- `--dry-run` sends no external traffic.

---

## Phase 12 — systemd and Verification

### `fand.service`

Most of what the subsystem needs was already in place:

| Requirement | Already satisfied by |
|---|---|
| Outbound sockets | `RestrictAddressFamilies=` already listed `AF_INET`/`AF_INET6` |
| No egress filtering | no `IPAddressDeny=`, no `PrivateNetwork=` |
| Worker threads | `SystemCallFilter=@system-service` includes `@network-io` |
| No privilege needed | outbound TCP to a high port needs no capability |
| Reload trigger | `ExecReload=/bin/kill -HUP $MAINPID` |
| Network availability at start | `After=`/`Wants=network-online.target` |
| Fan release backstop | `ExecStopPost=/usr/bin/ipmitool raw 0x30 0x30 0x01 0x01` |

**Two changes are required, though.**

#### `AF_NETLINK` for name resolution

`DiscordEndpoint` targets `https://discord.com`, so delivery needs DNS. glibc's `getaddrinfo()`
calls `__check_pf()`, which opens an **`AF_NETLINK`** socket to enumerate local addresses before
deciding whether to return IPv6 results. That family was not in the allow-list, so the call
returned `EPERM` (per `SystemCallErrorNumber`).

glibc is meant to degrade gracefully when that fails, and usually does — which is why this
presents as intermittent DNS failure rather than an obvious breakage. Adding `AF_NETLINK` costs
nothing and removes the ambiguity. The runtime checklist verifies resolution from inside the
sandbox, because it cannot be tested off the target host.

#### `EnvironmentFile=-` so a missing `.env` cannot stop fan control

`EnvironmentFile=` without a leading `-` on **the value** makes systemd **refuse to start the
unit** when the file is absent. `/opt/fand/.env` exists only to carry notification credentials.

The prefix goes before the path, not before the key:

```
EnvironmentFile=-/opt/fand/.env     # correct
-EnvironmentFile=/opt/fand/.env     # wrong: systemd logs "Unknown key" and IGNORES the line,
                                    # silently loading no environment at all
```

`systemd-analyze verify fand.service` catches the wrong form.

So before this change, a missing or mistyped notification credentials file stopped the fan-control
daemon from starting at all — the exact coupling this entire subsystem is designed to prevent.
With the `-` prefix on the path, an absent `.env` means notifiers are skipped with a logged warning and the
fans are controlled normally.

This defect predates the notification work; the subsystem is what made it consequential.

**Optional hardening, deliberately not applied:** `IPAddressAllow`/`IPAddressDeny` can restrict
egress to the Home Assistant host and Discord. Left out because Discord's address ranges change
without notice and the failure mode is a notifier that silently stops working. Worth adding in a
deployment that pins its own endpoints.

### Verification Checklist

Split by what can be proved off the target host and what cannot. Anything needing systemd,
`ipmitool`, or a real endpoint belongs in the second list and must not be reported as done until
it has actually been run on the machine.

#### Covered by the automated suite

These are asserted by `python -m unittest discover` and need no hardware:

- Both example configuration files parse and build through the factory.
- A malformed or invalid notifier file is skipped with one warning; the others load.
- An unset or empty environment variable is reported by name, with no secret in the message.
- A `[Credentials]` value that is not a variable name is rejected without logging it.
- Threshold rising edge, interval scheduling, re-arming, and sensor scoping.
- Queue overflow drops the oldest and warns; queue depth never exceeds capacity.
- Transient failures retry to `MaxAttempts`; permanent ones discard after one attempt.
- Reload reconciliation: added, removed, edited, and untouched — the untouched notifier keeps
  its worker and queued jobs.
- Teardown releases fan control before stopping notifiers, and each side still runs when the
  other raises.
- Signal handlers only set flags and emit no log records.
- `stop()` is bounded by one timeout regardless of how many notifiers are wedged.
- `--notify-test` exit codes for success, delivery failure, and unbuildable notifier.

#### Requires the physical host

- `systemctl start fand`: `READY=1`, watchdog heartbeat continues with notifications active.
- **DNS resolution from inside the sandbox** — the `AF_NETLINK` addition above. Confirm a Discord
  delivery actually resolves; this is the one change that cannot be tested off-target.
- A missing `/opt/fand/.env` starts the daemon normally with notifiers skipped, rather than
  failing the unit.
- `systemctl reload fand` with a file added, removed, edited, and untouched.
- `systemctl stop fand` with an unreachable endpoint: fans return to iDRAC automatic control
  within `TimeoutStopSec`.
- A real threshold crossing produces a colour-coded Discord embed and Home Assistant entities.
- `--dry-run` produces log output and zero network traffic (confirm with `ss`/`tcpdump`).
- Sustained endpoint failure: bounded memory and bounded thread CPU over hours.

#### Detail

**Configuration**

- Both example files are tracked by git.
- A notifier with a malformed `[Trigger]` is skipped with a warning; the others run.
- A notifier referencing an unset environment variable is skipped with a warning naming the
  variable and containing no secret.
- A `[Credentials]` value containing a literal secret is rejected without logging it.

**Endpoint delivery**

- `fand.py --notify-test` reports PASS for a correctly configured Discord notifier and a
  correctly configured Home Assistant notifier.
- A deliberately wrong token reports FAIL, classified permanent, with no retry storm.

**Trigger and scheduling**

- A threshold notifier queues immediately when the threshold is first crossed, then every
  `Interval` while it remains crossed, and stops when temperature falls below it.
- Re-crossing the threshold fires immediately again.
- A general notifier fires every `Interval` regardless of temperature.
- A notifier with `Sensors` set reports only those sensors; a missing name warns once and the
  rest are still delivered.

**Failure isolation**

- With Discord unreachable and Home Assistant healthy: Discord logs warnings and its queue fills
  and drops oldest; Home Assistant continues delivering normally; fan control is unaffected.
- Sustained endpoint failure produces bounded memory and bounded thread CPU.

**Reload**

- `systemctl reload fand`: added notifier starts, removed notifier stops, edited notifier
  restarts, untouched notifier keeps its worker and queue.
- A reload introducing a broken file leaves every valid notifier running.

**Lifecycle**

- `systemctl stop fand` with a hung endpoint: fans return to iDRAC automatic control, shutdown
  completes within `TimeoutStopSec`.
- Watchdog heartbeats continue uninterrupted with notifications active.
- `--dry-run` produces log output and zero network traffic (confirm with `ss`/`tcpdump`).

---

## Cross-Cutting Requirements

### Safety First

Notifications are best effort; cooling is not. Every failure path resolves toward "log it and
keep controlling the fans."

| Failure | Result |
|---|---|
| Endpoint unreachable | warning, bounded retry, job discarded |
| Queue full | oldest job dropped, warning |
| Invalid configuration | notifier skipped, warning |
| Missing credential | notifier skipped, warning naming the variable |
| Worker thread wedged | abandoned at shutdown, fan control already released |
| Notification bug raising | caught by the manager, and again by the controller |

### Concurrency Rules

The daemon is single-threaded today. These rules keep the addition of worker threads from
becoming a source of nondeterminism, per `CLAUDE.md`'s "predictable over clever":

- `State` is read only on the controller thread.
- Nothing mutable crosses a thread boundary; only immutable `Notification` snapshots do.
- Cross-thread communication is limited to `queue.Queue` and `threading.Event`, both thread-safe.
- Worker threads are `daemon=True` and never outlive a bounded join.
- No lock is held across an I/O call.
- Signal handlers set flags only.

### No Hardcoded Values

`Interval`, `QueueSize`, `MaxAttempts`, `RetryBackoff`, endpoint timeouts, entity prefixes,
trigger thresholds, and sensor selections are all configuration. The only fixed constants are
safety bounds that exist to make misconfiguration survivable: the maximum retry backoff, the
maximum queue size, and the worker join timeout.

### Secrets

Secrets exist in exactly two places: the environment, and private attributes of an endpoint
instance. They are never in a TOML file, never in a model object, never in a log record at any
level, and never in an exception message.

### Error Handling

No bare `except:`. Every caught exception is either handled or logged with context. Notification
failures are warnings, never fatal — `notification.md` and `CLAUDE.md` agree that this subsystem
must not be able to terminate the daemon.

---

## Testing Strategy

Tests live in `tests/`, mirroring the `lib/` layout, and run with stdlib `unittest`:

```
python -m unittest discover
```

`unittest` rather than `pytest` keeps the project dependency-free, per `CLAUDE.md`'s stdlib
preference. The suite was introduced in Phase 2, which is the first module that is pure logic with
no I/O to stub.

Consistent with `build_order.md`'s testing section, each layer must be verifiable in isolation:

- **Models** — validation rules, defaults, equality, and immutability, with no fixtures beyond a
  dict. The example configuration files are parsed as part of the suite so a schema change that
  breaks them fails a test rather than an operator's deployment.
- **Endpoints** — substitutable transport, so status-code classification is testable without a
  network.
- **Triggers** — pure functions of a `Notification`; no clock, no I/O.
- **Notifier** — a fake endpoint that succeeds, fails transiently, or fails permanently on demand
  covers retry bounds, queue overflow, and shutdown responsiveness.
- **Manager** — reload reconciliation is testable with configs alone.
- **End to end** — `--notify-test` and `--dry-run` provide the failure-injection and simulation
  paths `build_order.md` requires.

---

## Specification Revisions

Applied to `notification.md` in Phase 0. Items 1–3 and 6–8 resolve points the specification
explicitly delegates to the implementation; items 4, 5, 9, and 10 are design decisions taken for
this build.

1. **Units.** `Interval`, `RetryBackoff`, and all endpoint timeouts are **seconds**, matching
   `daemon.poll_interval`. Scheduling granularity is bounded by the poll interval: dispatch runs
   on the control loop, so an `Interval` shorter than `daemon.poll_interval` is effectively
   clamped to it, and configuration loading warns when that is the case.

2. **Notifier identity.** For reload reconciliation, a notifier is identified by its
   configuration file path. `Name` is documentation, diagnostics, and logging only, consistent
   with the specification's statement that it need not be unique.

3. **Threshold sensor selection.** `Sensors` is a property of `[Trigger]` for both trigger types.
   "The relevant sensor temperature" is defined as the hottest reading among the selected
   sensors, or among all sensors when `Sensors` is omitted — matching how `policy.py` evaluates
   the hottest sensor for fan decisions.

4. **`[Endpoint]` table.** A new optional table for non-secret, endpoint-specific options
   (request timeout, Home Assistant entity prefix), distinct from `[Credentials]`. The
   specification's "Endpoint-Specific Configuration" section already speaks of a
   credential/configuration schema; this separates the half that is not secret.

5. **Optional delivery keys.** `MaxAttempts` (default `3`) and `RetryBackoff` (default `1.0`)
   join the common properties, so `CLAUDE.md`'s no-hardcoded-values rule covers retry behaviour.
   Backoff is capped at 30 seconds by a fixed safety bound.

6. **`Enabled` default.** Omitting `Enabled` means `true`.

7. **Retry classification.** Transient (retryable): connection, DNS, TLS, timeout, `5xx`, and
   `429` honouring `Retry-After` up to the cap. Permanent (not retryable): all other `4xx`,
   including authentication and authorisation failures.

8. **Job lifecycle on stop.** Pending jobs are discarded when a notifier is stopped by daemon
   shutdown or by a reload that changed its configuration — consistent with the non-goals
   excluding guaranteed delivery and persistent queues.

9. **`--dry-run`.** Notifications are evaluated, scheduled, and logged, but never delivered.

10. **Transport security.** Endpoint URLs that are not `https` are permitted but warned about
    once per URL. TLS verification is never disabled.

11. **Operating mode.** Entering `WARNING` or `EMERGENCY` does not bypass a notifier's
    `Interval`. Notifiers fire only on their own trigger criteria and schedule, keeping the
    specification's rule that no notifier depends on state outside its own configuration.

---

## Appendix

### Why `lib/notifications/` is a new package

It is the notification equivalent of `lib/hardware/`: an abstract interface plus one module per
implementation, performing I/O and knowing nothing about daemon state. Placing endpoints in
`lib/hardware/` would put network services in a package `directory_map.md` describes as
"physical and virtual hardware." Placing the manager, factory, and model inside
`lib/notifications/` instead would break the repository's layer-based directory convention, in
which `managers/`, `factories/`, and `models/` hold all managers, factories, and models.

### Why the notification payload is an immutable snapshot

`State` is mutable and written every cycle by the controller thread. Any live view of it crossing
into a worker thread is a data race whose symptom is a notification reporting a reading that
never existed. Detaching a frozen snapshot at dispatch time makes the race impossible rather than
unlikely.

### Why the queue drops the oldest job

Specified by `notification.md`, and correct for this domain: a thermal notification's value
decays quickly. Ten-minute-old temperatures are not worth delivering ahead of current ones.

### Why notification configuration errors are non-fatal

The daemon cannot cool a machine without a fan curve, so `config.toml` errors are fatal. It cools
perfectly well without Discord. The specification requires that a bad notifier configuration not
prevent valid notifiers from loading and not affect fan control; a propagating `ConfigError`
would violate both.

### Why the transient/permanent error split exists

Without it, a single mistyped token consumes the full retry budget and its backoff on every job
forever. With it, permanent failures cost one attempt and one warning.

### Why `SIGHUP` handling changes

Signal handlers run on the main thread and can interrupt code holding the `logging` module lock.
The current handler calls `reload_config()`, which logs — a latent deadlock. Starting and joining
threads from a handler makes it materially worse. Setting a flag and reloading between cycles is
the standard fix.

### Why fan RPM is not in the payload

Nothing currently reads actual fan RPM; only the requested percentage is known. Adding
`IPMI.fan_readings()` would put an extra `ipmitool` invocation in the control loop's path for the
benefit of a non-critical subsystem. The payload reports the requested fan speed and the result
of the last fan command, which is what the daemon actually knows.
