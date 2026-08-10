# Architecture

## Philosophy

The project follows several core principles:

- Single Responsibility Principle
- Dependency Inversion
- Hardware Abstraction
- Configuration Driven
- Fail Safe by Default

The controller should coordinate components rather than perform work itself.

Controller
\>
SensorManager
\>
Sensors
\>
State
\>
Policy
\>
IPMI

## Layers

Application

- fand.py
- daemon.py

Orchestration

- Controller
- Managers (Sensor, VM, Config, Notification)

Business Logic

- Policy
- State

Hardware Abstraction Layer

- Sensor
- GPU
- IPMI

Notification Abstraction Layer

- Notification Endpoint
- Discord
- Home Assistant
- Trigger
- Notifier

Infrastructure

- Logging
- Retry
- QGA Communication
- HTTP Transport

The upper layers should never know how temperatures are collected or how fan speeds are written.

They should equally never know how a notification is formatted or transmitted. The Notification
Abstraction Layer sits beside the Hardware Abstraction Layer: both perform I/O only, and neither
knows anything about daemon state.

## Criticality

Fan control is critical. Notification is not.

The notification subsystem is subordinate to cooling. No layer above it may depend on its
success, and no failure within it — an unreachable endpoint, a full queue, an invalid
configuration, a wedged worker — may stop the fans from being controlled or delay the daemon
from releasing fan control on exit.