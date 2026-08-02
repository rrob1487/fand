"""Maintains one QEMU Guest Agent connection per monitored VM."""

from __future__ import annotations

from typing import Iterator

from lib.models.vm import VMConfig
from lib.utils.qga import QGAClient


class VMManager:
    """Owns VM configuration and their QGA connections."""

    def __init__(self, vms: dict[str, VMConfig]) -> None:
        self._vms = vms
        self._qga_clients: dict[str, QGAClient] = {
            name: QGAClient(vm.qga.socket) for name, vm in vms.items()
        }

    def qga_client(self, vm_name: str) -> QGAClient:
        return self._qga_clients[vm_name]

    def __iter__(self) -> Iterator[tuple[str, VMConfig]]:
        return iter(self._vms.items())
