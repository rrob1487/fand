"""QEMU Guest Agent communication layer.

Talks to a qemu-ga UNIX socket using its JSON-based RPC protocol. Callers
supply the command to run inside the guest; this module has no knowledge
of what that command is or how to interpret its output.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)


class QGAError(Exception):
    """Raised when the guest agent returns an error response."""


class QGATimeoutError(QGAError):
    """Raised when a guest-exec command does not complete in time."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int | None
    signal: int | None
    stdout: str
    stderr: str


def _decode_b64(data: str | None) -> str:
    if not data:
        return ""
    return base64.b64decode(data).decode(errors="replace")


class QGAClient:
    """Client for a single VM's QEMU Guest Agent socket."""

    def __init__(self, socket_path: str | Path, timeout: float = 5.0) -> None:
        self._socket_path = str(socket_path)
        self._timeout = timeout

    def _call(self, execute: str, arguments: dict | None = None) -> dict:
        payload: dict = {"execute": execute}
        if arguments is not None:
            payload["arguments"] = arguments

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self._timeout)
            sock.connect(self._socket_path)
            sock.sendall(json.dumps(payload).encode())
            raw = self._read_response(sock)

        response = json.loads(raw)
        if "error" in response:
            raise QGAError(response["error"].get("desc", str(response["error"])))
        return response.get("return", {})

    def _read_response(self, sock: socket.socket) -> bytes:
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            try:
                json.loads(b"".join(chunks))
                break
            except json.JSONDecodeError:
                continue
        return b"".join(chunks)

    def guest_exec(
        self, path: str, arg: list[str] | None = None, capture_output: bool = True,
    ) -> int:
        """Start a command in the guest. Returns the guest-side pid."""
        arguments: dict = {"path": path, "capture-output": capture_output}
        if arg:
            arguments["arg"] = list(arg)
        result = self._call("guest-exec", arguments)
        return result["pid"]

    def guest_exec_status(self, pid: int) -> dict:
        """Raw guest-exec-status response for a pid returned by guest_exec."""
        return self._call("guest-exec-status", {"pid": pid})

    def exec_and_wait(
        self,
        path: str,
        arg: list[str] | None = None,
        poll_interval: float = 0.2,
        timeout: float = 5.0,
    ) -> ExecResult:
        """Run a command in the guest and block until it exits."""
        pid = self.guest_exec(path, arg)
        deadline = time.monotonic() + timeout
        while True:
            status = self.guest_exec_status(pid)
            if status.get("exited"):
                return ExecResult(
                    exit_code=status.get("exitcode"),
                    signal=status.get("signal"),
                    stdout=_decode_b64(status.get("out-data")),
                    stderr=_decode_b64(status.get("err-data")),
                )
            if time.monotonic() >= deadline:
                raise QGATimeoutError(
                    f"guest-exec pid {pid} ({path}) did not exit within {timeout}s"
                )
            time.sleep(poll_interval)
