# Directory Map

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

## Root

| Path | Purpose |
|------|---------|
| `fand.py` | Application entry point. |
| `.env` | Environment-specific variables. |
| `.env.example` | Checked-in template for `.env`. Contains no values. |
| `config/` | User-editable configuration. |
| `lib/` | Application source code. |
| `docs/` | Project documentation. |

## config/

| Path                         | Purpose |
|------------------------------|---------|
| `config.toml.example`        | Global daemon configuration. |
| `vms/`                       | VM definitions. |
| `vm.toml.example`            | Example VM configuration. |
| `notification/`              | Notifier definitions. One file per notifier. |
| `discord.toml.example`       | Example Discord notifier configuration. |
| `homeassistant.toml.example` | Example Home Assistant notifier configuration. |

## docs/

| Document                        | Description                                        |
|---------------------------------|----------------------------------------------------|
| `architecture.md`               | Overall system architecture and design philosophy  |
| `design_principles.md`          | Description of how new code should be written      |
| `directory_map.md`              | Description of every directory and source file     |
| `build_order.md`                | Recommended implementation roadmap                 |
| `notification_build_order.md`   | Notification subsystem implementation roadmap      |
| `control_loop.md`               | One iteration of the daemon explained              |
| `state_machine.md`              | Runtime operating states and transitions           |
| `notification.md`               | Describes the functionality of notification system |
| `configuration.md`              | Configuration file reference                       |
| `diagrams/class_diagram.md`     | High-level object relationships                    |
| `diagrams/control_flow.md`      | Runtime control flow                               |
| `diagrams/notification_flow.md` | Notification dispatch and delivery flow            |
| `diagrams/startup.md`           | Startup sequence                                   |

## lib/

| File | Responsibility |
|------|----------------|
| `daemon.py` | Daemon lifecycle. |
| `controller.py` | Coordinates subsystems. |
| `policy.py` | Computes desired fan speed. |
| `state.py` | Stores runtime state. |

### lib/hardware/

Responsible for interacting with physical and virtual hardware.

| File | Responsibility |
|------|----------------|
| `sensor.py` | Abstract base class. |
| `gpu.py` | GPU telemetry. |
| `ipmi.py` | Dell IPMI interface. |

### lib/notifications/

Responsible for delivering notifications to external services. The notification counterpart of
`lib/hardware/`: I/O only, with no knowledge of daemon state.

| File | Responsibility |
|------|----------------|
| `notification.py` | Generic notification payload. |
| `endpoint.py` | Abstract endpoint interface. |
| `discord.py` | Discord endpoint. |
| `homeassistant.py` | Home Assistant endpoint. |
| `trigger.py` | Trigger evaluation. |
| `notifier.py` | Queue, worker, and delivery for one notifier. |

### lib/managers/

Managers own collections of runtime objects.

| File | Responsibility |
|------|----------------|
| `sensor_manager.py` | Polls sensors. |
| `config_manager.py` | Loads configuration. |
| `vm_manager.py` | Maintains VM connections. |
| `notification_manager.py` | Owns notifiers and routes notifications. |

### lib/models/

Represents configuration as Python objects.

| File | Responsibility |
|------|----------------|
| `config.py` | Daemon configuration. |
| `vm.py` | VM configuration. |
| `notification.py` | Notifier configuration. |

### lib/factories/

Creates runtime objects from configuration.

| File | Responsibility |
|------|----------------|
| `sensor_factory.py` | Creates sensors. |
| `notifier_factory.py` | Creates notifiers. |

### lib/utils/

Reusable helper utilities.

| File | Responsibility |
|------|----------------|
| `qga.py` | QEMU Guest Agent client. |
| `retry.py` | Retry and backoff. |
| `http.py` | JSON-over-HTTP transport. |
| `logging.py` | Logging configuration. |
