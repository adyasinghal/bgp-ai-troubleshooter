"""
Device client — transport layer between the tool cohort and network devices.

Two transports are available:

  FRRDeviceClient  — SSH via Paramiko; runs vtysh on Containerlab FRR nodes.
                     Use this for the local lab (default).
  RESTDeviceClient — HTTP POST to a device's command-execution REST API.
                     Use this when devices expose a REST endpoint.

Both implement DeviceClientProtocol (run_vtysh / run_raw → DeviceResult).
Tools and BaseTool depend only on that protocol — they never import a concrete
client directly.

Transport selection
-------------------
Call make_device_client() to get the right client for the current environment.
It reads DEVICE_TRANSPORT ("ssh" | "rest", default "ssh").
REST-specific knobs: DEVICE_REST_PORT (default 8080), DEVICE_REST_SCHEME (default http).
"""

import os
import shlex
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

import paramiko
import requests as _requests


@dataclass
class DeviceResult:
    host: str
    command: str
    success: bool
    output: str
    error: Optional[str] = None


@runtime_checkable
class DeviceClientProtocol(Protocol):
    """Common interface every device client must satisfy."""

    def run_vtysh(self, host: str, command: str) -> DeviceResult: ...
    def run_raw(self, host: str, command: str) -> DeviceResult: ...


class FRRDeviceClient:
    def __init__(self, username: str = "admin", password: str = "admin",
                 key_filename: Optional[str] = None, port: int = 22, timeout: int = 8):
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.port = port
        self.timeout = timeout

    def run_vtysh(self, host: str, command: str) -> DeviceResult:
        full_cmd = f"vtysh -c {shlex.quote(command)}"
        return self._run(host, full_cmd, display_command=command)

    def run_raw(self, host: str, command: str) -> DeviceResult:
        return self._run(host, command, display_command=command)

    def _run(self, host: str, shell_command: str, display_command: str) -> DeviceResult:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs: dict = dict(
                hostname=host, port=self.port,
                username=self.username, timeout=self.timeout,
            )
            if self.key_filename:
                connect_kwargs["key_filename"] = self.key_filename
            else:
                connect_kwargs["password"] = self.password

            client.connect(**connect_kwargs)
            _, stdout, stderr = client.exec_command(shell_command, timeout=self.timeout)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            exit_status = stdout.channel.recv_exit_status()
            return DeviceResult(
                host=host, command=display_command,
                success=(exit_status == 0),
                output=out.strip(), error=err.strip() or None,
            )
        except Exception as e:
            return DeviceResult(
                host=host, command=display_command,
                success=False, output="",
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            client.close()


class RESTDeviceClient:
    def __init__(self, port: int = 8080, scheme: str = "http",
                 timeout: int = 8, headers: Optional[dict] = None):
        self.port = port
        self.scheme = scheme
        self.timeout = timeout
        self.headers = headers or {"Content-Type": "application/json"}

    def run_vtysh(self, host: str, command: str) -> DeviceResult:
        return self._post(host, {"type": "vtysh", "command": command})

    def run_raw(self, host: str, command: str) -> DeviceResult:
        return self._post(host, {"type": "shell", "command": command})

    def _post(self, host: str, payload: dict) -> DeviceResult:
        command = payload["command"]
        url = f"{self.scheme}://{host}:{self.port}/command"
        try:
            r = _requests.post(url, json=payload, headers=self.headers, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
            return DeviceResult(
                host=host, command=command,
                success=data.get("success", True),
                output=data.get("output", ""),
                error=data.get("error") or None,
            )
        except Exception as e:
            return DeviceResult(
                host=host, command=command,
                success=False, output="",
                error=f"{type(e).__name__}: {e}",
            )


def make_device_client() -> DeviceClientProtocol:
    transport = os.getenv("DEVICE_TRANSPORT", "ssh").lower()
    if transport == "rest":
        return RESTDeviceClient(
            port=int(os.getenv("DEVICE_REST_PORT", "8080")),
            scheme=os.getenv("DEVICE_REST_SCHEME", "http"),
        )
    return FRRDeviceClient()
