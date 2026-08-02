# `docs/design_principles.md`

# Design Principles

This document defines the architectural philosophy of **fand**. It exists to ensure that new features integrate naturally into the existing codebase without introducing unnecessary coupling or violating the project's design goals.

When adding new functionality, prefer extending the existing architecture over creating special-case logic.

---

# Core Principles

## Safety First

The primary responsibility of `fand` is protecting hardware.

Whenever a decision must be made between hardware safety and convenience, choose the safer option.

Examples include:

* Failing closed rather than open
* Preferring maximum fan speed over insufficient cooling
* Gracefully handling communication failures
* Recovering automatically whenever possible

---

## Single Responsibility Principle

Each class should have exactly one reason to change.

For example:

* Controllers coordinate work.
* Managers own collections of objects.
* Policies make decisions.
* Hardware classes communicate with devices.
* Models represent data.
* Factories construct objects.

Avoid creating "God Objects" that perform multiple unrelated tasks.

---

## Separation of Concerns

Business logic should never contain hardware-specific code.

For example:

Good:

```python
target = policy.calculate(state)
ipmi.set_fan_speed(target)
```

Avoid:

```python
if gpu.temperature > 70:
    ipmitool.raw(...)
```

The policy decides *what* should happen.

The hardware layer decides *how* it happens.

---

## Hardware Abstraction

All hardware communication should occur through dedicated interfaces.

The rest of the daemon should never know:

* how GPU temperatures are collected
* how fan speeds are written
* how QEMU Guest Agent works
* how IPMI commands are encoded

This allows implementations to change without affecting higher layers.

---

## Configuration Driven

Behavior should be configured rather than hard-coded.

Adding another VM should require creating a configuration file rather than modifying Python code.

Configuration should describe the system.

The code should implement the behavior.

---

## Composition Over Conditionals

Prefer polymorphism over long `if` statements.

Good:

```python
for sensor in sensors:
    sensor.read()
```

Avoid:

```python
if sensor.type == "gpu":
    ...
elif sensor.type == "ipmi":
    ...
```

New sensor types should require adding new classes rather than modifying existing logic.

---

## Dependency Direction

Dependencies should always point downward.

```
fand.py
    ↓
Daemon
    ↓
Controller
    ↓
Managers
    ↓
Hardware
```

Lower layers should never import higher layers.

For example:

✔ `Controller → SensorManager`

✘ `SensorManager → Controller`

---

# Responsibilities

## Entry Point

Responsible for:

* parsing command-line arguments
* configuring logging
* loading environment variables
* creating the daemon

Should never contain application logic.

---

## Daemon

Responsible for:

* startup
* shutdown
* signal handling
* watchdog notifications
* execution of the control loop

Should not contain fan-control logic.

---

## Controller

Responsible for coordinating application components.

The controller should remain intentionally small.

A typical control cycle should resemble:

```
poll sensors

↓

update state

↓

evaluate policy

↓

apply fan speed

↓

notify watchdog
```

The controller should orchestrate work rather than perform it.

---

## Managers

Managers own groups of runtime objects.

Examples:

* SensorManager
* VMManager
* ConfigManager

Managers are responsible for object lifecycle, not decision making.

---

## Policy

The policy layer answers one question:

> Given the current state, what should the fans do?

It should not communicate with hardware directly.

It returns a desired fan speed.

---

## State

State is the shared representation of the system.

It should contain:

* current temperatures
* timestamps
* alarms
* operating mode
* desired fan speed

State should never communicate with hardware.

---

## Hardware Layer

Hardware classes perform I/O only.

Examples:

* read GPU temperatures
* write fan speeds
* query platform sensors

They should contain little or no business logic.

---

## Factories

Factories create runtime objects from configuration.

The rest of the daemon should never manually instantiate implementation-specific classes.

Example:

```
SensorFactory

↓

GPUSensor
```

---

## Models

Models provide strongly typed representations of configuration and runtime objects.

Avoid passing raw dictionaries throughout the application.

---

## Utilities

Utility modules should be:

* stateless
* reusable
* independent

They should never depend on application state.

---

# Error Handling

Recoverable errors should be logged and retried.

Fatal errors should immediately trigger the daemon's safety mechanisms.

Never silently ignore exceptions involving hardware communication.

---

# Extensibility

Adding support for new hardware should require:

1. A new implementation.
2. Registration with the appropriate factory.
3. A configuration entry.

Existing code should require little or no modification.

---

# Code Style

Prefer readability over cleverness.

Write code that explains itself.

Avoid premature optimization.

Document *why* something is done rather than *what* the code already says.

Keep methods short.

Prefer many small classes over a few very large ones.

---

# Long-Term Goal

The ideal architecture is one where adding support for a new hypervisor, GPU vendor, or sensor type requires extending the system rather than rewriting it.

If a new feature requires modifying large portions of the existing codebase, reconsider the design before implementing it.
