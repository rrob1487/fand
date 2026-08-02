"""GPU temperature sensor.

nvidia-smi runs inside the guest VM via QEMU Guest Agent guest-exec; its
output is parsed here, on the host.
"""

from __future__ import annotations

from lib.hardware.sensor import Sensor
from lib.utils.qga import QGAClient, QGAError

NVIDIA_SMI_PATH = "/usr/bin/nvidia-smi"
NVIDIA_SMI_ARGS = ["--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"]


class GPUReadError(Exception):
    """Raised when a GPU temperature reading cannot be obtained or parsed."""


class GPUSensor(Sensor):
    """Reads GPU temperature by running nvidia-smi inside a VM guest."""

    def __init__(self, qga: QGAClient, timeout: float = 5.0) -> None:
        self._qga = qga
        self._timeout = timeout

    def read(self) -> float:
        try:
            result = self._qga.exec_and_wait(
                NVIDIA_SMI_PATH, NVIDIA_SMI_ARGS, timeout=self._timeout,
            )
        except QGAError as exc:
            raise GPUReadError(f"nvidia-smi guest-exec failed: {exc}") from exc

        if result.exit_code != 0:
            raise GPUReadError(
                f"nvidia-smi exited with {result.exit_code}: {result.stderr.strip()}"
            )
        return self._parse_temperature(result.stdout)

    @staticmethod
    def _parse_temperature(output: str) -> float:
        first_line = output.strip().splitlines()[0] if output.strip() else ""
        try:
            return float(first_line.strip())
        except ValueError as exc:
            raise GPUReadError(f"unparsable nvidia-smi output: {output!r}") from exc
