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
- Managers

Business Logic

- Policy
- State

Hardware Abstraction Layer

- Sensor
- GPU
- IPMI

Infrastructure

- Logging
- Retry
- QGA Communication

The upper layers should never know how temperatures are collected or how fan speeds are written.