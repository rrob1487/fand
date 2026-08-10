# Build Order

## Purpose

This document is the implementation roadmap for `fand`, a fan-control daemon for Dell PowerEdge servers running GPU workloads inside QEMU virtual machines.

The goal of this document is to define the order in which the project should be implemented while preserving the architecture defined in:

- `architecture.md`
- `design_principles.md`
- `directory_map.md`
- `configuration.md`
- `control_loop.md`
- `state_machine.md`

The build order follows the dependency direction defined by the architecture. Lower-level components must be completed before higher-level orchestration components are built. Every file created in this document appears in `directory_map.md` — nothing is added beyond it except the `__init__.py` package files noted in Phase 0, which are a mechanical necessity of the directory structure `directory_map.md` already specifies, not a new component.

The notification subsystem defined in `notification.md` has its own roadmap:
`notification_build_order.md`. This document covers the fan-control daemon; that one covers
notifications and assumes every phase here is complete.

---

## Build Philosophy

`fand` follows a bottom-up implementation strategy.

The dependency direction is:

```
Application Layer
        |
        v
Orchestration Layer
        |
        v
Business Logic Layer
        |
        v
Hardware Abstraction Layer
        |
        v
Infrastructure Layer
```

Allowed dependency flow:

```
fand.py
    |
    v
daemon.py
    |
    v
controller.py
    |
    v
managers/
    |
    v
models/
    |
    v
hardware/
    |
    v
utils/
```

Forbidden dependencies:

```
hardware  → controller
policy    → ipmi
sensor    → config_manager
utils     → application code
```

Lower layers must never import or depend on higher layers.

---

## Phase 0 — Project Scaffolding

**Purpose:** create the project structure, configuration schema, and Python package layout.

**Additional files:** Python `__init__.py` files are required even though they are not explicitly shown in `directory_map.md`. They are required because the directory structure implies Python packages.

### `.env`

**Responsibility:** stores environment-specific values that should not exist in configuration files.

Examples:
- secrets
- runtime overrides
- environment flags

**Must provide:**
- Environment loading support

**Must NOT do:**
- Store hardware configuration
- Store VM definitions

### `config/config.toml.example`

**Responsibility:** defines daemon-wide configuration.

**Must provide** — example configuration for:
- polling interval
- logging level
- fan curve
- safety thresholds
- watchdog configuration

Example:

```toml
[daemon]
poll_interval = 5
log_level = "INFO"

[safety]
max_temperature = 90

[watchdog]
enabled = true
```

> Note: this file does not define an "operating mode" key. Operating mode (`STARTING`/`RUNNING`/`WARNING`/`EMERGENCY`) is runtime-derived state owned by `state.py` (see Phase 5), not something a user configures.

### `config/vms/*.toml.example`

**Responsibility:** defines VM-specific monitoring configuration.

**Must provide:**
- VM name
- QEMU guest agent socket
- sensor type
- GPU mappings
- temperature limits

Example:

```toml
[name]
vm = "n8n"

[qga]
socket = "/run/qemu/qemu-n8n-ga.sock"

[gpu]
type = "nvidia"
```

### Python Package Initialization

Create:

```
lib/__init__.py
lib/hardware/__init__.py
lib/managers/__init__.py
lib/models/__init__.py
lib/factories/__init__.py
lib/utils/__init__.py
```

**Responsibility:** enable Python package imports.

---

## Phase 1 — Infrastructure Layer

**Directory:** `lib/utils/`

Infrastructure provides reusable primitives.

### `lib/utils/logging.py`

**Responsibility:** centralized logging configuration.

**Depends on:** nothing.

**Must provide:**
- journald-friendly formatting
- log level configuration
- module logging helpers

**Must NOT do:**
- Know about hardware
- Know about daemon state

### `lib/utils/retry.py`

**Responsibility:** generic retry mechanisms.

**Depends on:** nothing.

**Must provide:**
- retry decorators/helpers
- backoff behavior

**Must NOT do:**
- Handle hardware-specific failures
- Decide recovery behavior

### `lib/utils/qga.py`

**Responsibility:** QEMU Guest Agent communication layer.

**Depends on:** socket standard library.

**Must provide:**
- guest-exec execution
- response parsing
- socket handling

**Must NOT do:**
- Know about GPUs
- Decide temperature policy

**Phase Completion Criteria:**
- QGA can communicate with a VM.
- Retry logic works independently.
- Logging works through journald.

---

## Phase 2 — Models

**Directory:** `lib/models/`

Models represent structured data.

### `lib/models/config.py`

**Responsibility:** typed representation of configuration files.

**Depends on:** Phase 0 configuration schema.

**Must provide:**
- daemon configuration models
- safety settings
- fan curve configuration

**Must NOT do:**
- Perform hardware operations

### `lib/models/vm.py`

**Responsibility:** represents monitored VM configuration.

**Must provide:**
- VM metadata
- QGA connection information
- GPU mappings

**Must NOT do:**
- Connect to QGA directly

---

## Phase 3 — Hardware Abstraction

**Directory:** `lib/hardware/`

Hardware classes perform I/O only. They must not contain business logic.

### `lib/hardware/sensor.py`

**Responsibility:** abstract sensor interface.

**Must provide:**

```python
read()
```

**Must NOT do:**
- Know about specific hardware

### `lib/hardware/gpu.py`

**Responsibility:** GPU temperature sensor implementation.

**Depends on:**
- `utils/qga.py`
- `sensor.py`

**Must provide:**
- GPU temperature reading
- nvidia-smi parsing (run inside the guest via QGA guest-exec, parsed on the host)

**Must NOT do:**
- Decide fan speed

### `lib/hardware/ipmi.py`

**Responsibility:** Dell IPMI interface.

Contains:

```python
class IPMI:
    raw_command()
    sensor_readings()
    temperature_sensor_names()

class IPMISensor(Sensor):
    read()

class IPMIFanController:
    set_speed()
```

**Must provide:**
- temperature reading
- fan speed control
- Dell raw command support
- `temperature_sensor_names()`: names of every BMC sensor reporting a
  temperature unit (e.g. `"degrees C"` in `ipmitool sensor`'s output),
  excluding non-temperature numeric sensors (fan RPM, voltage, power
  draw, ...). Sensor count and naming vary by chassis — some boards
  report two identically-named `"Temp"` sensors for dual CPUs — so this
  is discovered from hardware at runtime rather than hand-enumerated in
  configuration. Disambiguates repeated names in encounter order:
  `"Temp"`, `"Temp #2"`, ...

**Must NOT do:**
- Decide cooling policy

**Phase Completion Criteria:**
- GPU temperatures can be queried.
- IPMI temperatures can be queried.
- Manual fan commands work.
- Hardware failures are surfaced.

---

## Phase 4 — Factories

**Directory:** `lib/factories/`

### `lib/factories/sensor_factory.py`

**Responsibility:** create hardware implementations from configuration.

**Depends on:**
- models
- hardware classes

**Must provide:**

```python
create_gpu_sensor(vm: VMConfig, timeout: float = 5.0) -> GPUSensor
discover_ipmi_sensors(ipmi: IPMI) -> list[IPMISensor]
```

`create_gpu_sensor` builds a `GPUSensor` from a VM's configuration (one
GPU sensor per VM, matching `vm.toml`). `discover_ipmi_sensors` calls
`IPMI.temperature_sensor_names()` and builds one `IPMISensor` per name
reported — IPMI sensor count and naming is hardware-specific, not
something `config.toml` enumerates.

**Must NOT do:**
- Contain temperature policy

**Phase Completion Criteria:** adding a new sensor requires:
1. New implementation
2. Factory registration
3. Configuration entry

No controller changes.

---

## Phase 5 — Business Logic

### `lib/state.py`

**Responsibility:** stores the current truth of the system.

**Must provide:**
- temperatures
- timestamps
- alarms
- operating mode (`STARTING` / `RUNNING` / `WARNING` / `EMERGENCY`, per `state_machine.md`)
- requested fan speed
- last hardware command result

State should contain data only.

Bad:

```python
state.should_raise_fans()
```

Good:

```python
policy.evaluate(state)
```

### `lib/policy.py`

**Responsibility:** convert system state into desired fan behavior — including safety evaluation and emergency handling. Per `control_loop.md`, "evaluate safety conditions" sits directly in the poll → state → **safety** → fan-speed sequence, and `architecture.md`'s Business Logic layer names only Policy and State — so emergency/safety logic lives here rather than in a separate module.

**Must provide:**
- fan curve evaluation
- hysteresis
- safety threshold evaluation
- emergency detection and transition (drives `state.py`'s operating mode into `WARNING`/`EMERGENCY` per `state_machine.md`)
- emergency fan command (maximum fan speed override)
- shutdown request signaling (a flag/result the Controller/Daemon can act on, per `state_machine.md`'s "shut down host if configured" — Policy signals it, it does not execute it)

**Must NOT do:**
- Access hardware
- Execute IPMI commands

**Phase Completion Criteria:**
- Given a `State` object, policy returns a fan decision (including emergency overrides).
- No hardware calls exist.

---

## Phase 6 — Managers

**Directory:** `lib/managers/`

Managers own groups of runtime objects. They coordinate but do not make decisions.

### `lib/managers/config_manager.py`

**Responsibility:** loads configuration.

**Must provide:**
- config parsing
- VM discovery
- reload support

### `lib/managers/vm_manager.py`

**Responsibility:** maintains VM connections.

**Depends on:**
- VM models
- QGA utilities

### `lib/managers/sensor_manager.py`

**Responsibility:** maintains sensor objects.

**Depends on:**
- sensor factory
- VM manager

**Phase Completion Criteria:**
- Configuration loads.
- VM sensors are created automatically.
- Sensor polling works.

---

## Phase 7 — Controller

### `lib/controller.py`

**Responsibility:** main orchestration loop.

Controller coordinates:

```
SensorManager
       |
       v
State
       |
       v
Policy (safety check + fan decision)
       |
       v
Fan Controller
```

Control cycle:
1. Poll sensors
2. Update state
3. Evaluate safety (via Policy)
4. Compute desired fan speed (via Policy)
5. Apply hysteresis
6. Set fan speed
7. Kick watchdog

Controller should remain small.

**Phase Completion Criteria:**
- Full control loop executes.
- Controller contains no hardware parsing.
- Controller contains no configuration loading.
- Controller contains no safety/emergency logic of its own — it only acts on what `Policy` returns.

---

## Phase 8 — Application Layer

### Refactor `lib/daemon.py`

Move:
- argument parsing
- logging setup
- environment loading

into `fand.py`.

Daemon responsibilities:
- systemd lifecycle
- sd_notify
- watchdog handling
- controller lifecycle

### Create `fand.py`

**Responsibility:** application entry point.

Provides:
- CLI arguments
- logging initialization
- daemon creation

Startup sequence:

```
systemd
   |
   v
fand.py
   |
   v
Daemon
   |
   v
Managers initialized
   |
   v
READY=1
   |
   v
Controller loop
```

---

## Phase 9 — Integration

Update `fand.service`.

Change:

```
ExecStart=/usr/bin/python3 /opt/fand/lib/daemon.py
```

to:

```
ExecStart=/usr/bin/python3 /opt/fand/fand.py
```

### Verification Checklist

**Simulation Mode** — required behavior:

```
Read sensors
    |
Calculate decisions
    |
Log proposed changes
    |
Do not write hardware
```

**Runtime Tests:**
- Start daemon
- Verify READY notification
- Verify watchdog heartbeat
- Verify SIGHUP reload
- Verify emergency behavior
- Verify maximum cooling response

---

## Cross-Cutting Requirements

### Safety First

Failures must fail closed.

Examples:
- Unknown temperature → increase cooling
- Hardware failure → maximum safe cooling
- Lost daemon → systemd recovery

### Error Handling

Recoverable:

```
retry.py
   |
  log
   |
retry
```

Fatal:

```
trigger safety response
```

Never silently ignore hardware failures.

### Configuration Driven

Adding:
- new VM
- new GPU
- new sensor

should require configuration changes only.

---

## Testing Strategy

Every major layer should support isolated testing.

Required:
- hardware mocks
- simulation mode
- dry-run mode
- failure injection testing

---

## Appendix

### `__init__.py`

Added because Python package structure requires explicit package initialization.

### IPMI Dual Role

`ipmi.py` contains:
- Dell IPMI communication
- IPMI sensor adapter
- IPMI fan controller

This keeps Dell-specific behavior together while preserving abstraction boundaries.

### Safety Handling

Emergency/safety logic is implemented inside `policy.py` rather than a separate `safety.py` file. `directory_map.md` does not list a safety module, and `architecture.md`'s Business Logic layer names only Policy and State — keeping safety evaluation inside `policy.py` avoids introducing a component the existing architecture docs don't define.

### Documentation Note

Existing documentation references:

```
control_flow.md
```

but the file currently exists as:

```
controll_flow.md
```
