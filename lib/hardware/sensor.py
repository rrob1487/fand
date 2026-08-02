"""Abstract sensor interface. Hardware classes perform I/O only."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Sensor(ABC):
    @abstractmethod
    def read(self) -> float:
        """Return the current sensor reading."""
