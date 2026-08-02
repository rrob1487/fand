# `docs/README.md`

# fand Documentation

Welcome to the **fand** documentation.

`fand` is a fan-control daemon designed specifically for Dell PowerEdge servers running GPU workloads inside QEMU virtual machines. Rather than relying solely on the iDRAC's thermal model, `fand` combines platform sensor data with GPU temperatures gathered through the QEMU Guest Agent to make intelligent cooling decisions.

## Design Goals

* Safety above everything else
* Modular, object-oriented architecture
* Hardware abstraction through well-defined interfaces
* Support for multiple GPU VMs
* Hot-reloadable configuration
* Minimal runtime dependencies
* Graceful failure handling
* Easy to extend for additional sensor types

## Documentation

| Document                    | Description                                       |
| --------------------------- | ------------------------------------------------- |
| `architecture.md`           | Overall system architecture and design philosophy |
| `directory_map.md`          | Description of every directory and source file    |
| `build_order.md`            | Recommended implementation roadmap                |
| `control_loop.md`           | One iteration of the daemon explained             |
| `configuration.md`          | Configuration file reference                      |
| `state_machine.md`          | Runtime operating states and transitions          |
| `diagrams/class_diagram.md` | High-level object relationships                   |
| `diagrams/control_flow.md`  | Runtime control flow                              |
| `diagrams/startup.md`       | Startup sequence                                  |
