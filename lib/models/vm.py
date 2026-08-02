"""Typed representation of a monitored VM's configuration (vms/*.toml)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QGAConnection:
    socket: str

    @classmethod
    def from_dict(cls, data: dict) -> "QGAConnection":
        return cls(socket=data["socket"])


@dataclass(frozen=True)
class GPUMapping:
    type: str

    @classmethod
    def from_dict(cls, data: dict) -> "GPUMapping":
        return cls(type=data["type"])


@dataclass(frozen=True)
class VMLimits:
    max_temperature: float

    @classmethod
    def from_dict(cls, data: dict) -> "VMLimits":
        return cls(max_temperature=data["max_temperature"])


@dataclass(frozen=True)
class VMConfig:
    name: str
    qga: QGAConnection
    gpu: GPUMapping
    limits: VMLimits

    @classmethod
    def from_dict(cls, data: dict) -> "VMConfig":
        return cls(
            name=data["name"]["vm"],
            qga=QGAConnection.from_dict(data["qga"]),
            gpu=GPUMapping.from_dict(data["gpu"]),
            limits=VMLimits.from_dict(data["limits"]),
        )
