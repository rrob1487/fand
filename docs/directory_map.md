# Directory Map

```text
fand/
├── fand.py
├── .env
├── config/
│   ├── config.toml.example 
│   └── vms/
│       └── vm.toml.example
├── lib/
│   ├── daemon.py
│   ├── controller.py
│   ├── policy.py
│   ├── state.py 
│   ├── hardware/ 
│   │   ├── sensor.py 
│   │   ├── ipmi.py 
│   │   └── gpu.py
│   ├── managers/
│   │   ├── sensor_manager.py
│   │   ├── config_manager.py
│   │   └── vm_manager.py
│   ├── models/ 
│   │   ├── config.py
│   │   └── vm.py 
│   ├── factories/
│   │   └── sensor_factory.py
│   └── utils/ 
│       ├── qga.py 
│       ├── retry.py 
│       └── logging.py
└── docs/
    ├── directory_map.md
    ├── build_order.md
    └── architecture.md
```

## Root

| Path | Purpose |
|------|---------|
| `fand.py` | Application entry point. |
| `.env` | Environment-specific variables. |
| `config/` | User-editable configuration. |
| `lib/` | Application source code. |
| `docs/` | Project documentation. |

## config/

| Path                  | Purpose |
|-----------------------|---------|
| `config.toml.example` | Global daemon configuration. |
| `vms/`                | VM definitions. |
| `vm.toml.example`     | Example VM configuration. |

## docs/

| Document                    | Description                                       |
|-----------------------------|---------------------------------------------------|
| `architecture.md`           | Overall system architecture and design philosophy |
| `design_principles.md`      | Description of how new code should be written     |
| `directory_map.md`          | Description of every directory and source file    |
| `build_order.md`            | Recommended implementation roadmap                |
| `control_loop.md`           | One iteration of the daemon explained             |
| `configuration.md`          | Configuration file reference                      |
| `state_machine.md`          | Runtime operating states and transitions          |
| `diagrams/class_diagram.md` | High-level object relationships                   |
| `diagrams/control_flow.md`  | Runtime control flow                              |
| `diagrams/startup.md`       | Startup sequence                                  |

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

### lib/managers/

Managers own collections of runtime objects.

| File | Responsibility |
|------|----------------|
| `sensor_manager.py` | Polls sensors. |
| `config_manager.py` | Loads configuration. |
| `vm_manager.py` | Maintains VM connections. |

### lib/models/

Represents configuration as Python objects.

### lib/factories/

Creates runtime objects from configuration.

### lib/utils/

Reusable helper utilities.