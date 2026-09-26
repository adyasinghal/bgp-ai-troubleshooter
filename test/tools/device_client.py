"""
Device client — the "REST API layer" that normalizes commands per vendor/OS
and reaches Network devices via SSH (matches the bottom of the architecture
diagram).

For this project devices are FRR routers running in Containerlab. FRR exposes
its CLI through `vtysh`, so we SSH to the container's management IP and run
`vtysh -c "<command>"`.
"""

import shlex
from dataclasses import dataclass
from typing import Optional

import paramiko


@dataclass
class DeviceResult:
    host: str
    command: str
    success: bool
    output: str
    error: Optional[str] = None


class FRRDeviceClient:
    """
    SSH transport to a Containerlab FRR node.

    Containerlab FRR nodes (vendor: frr) default to username 'admin' /
    password 'admin' or key-based auth depending on your clab topology file
    -- adjust `username`/`password`/`key_filename` to match your lab's
    node config.
    """

    def __init__(self, username: str = "admin", password: str = "admin",
                 key_filename: Optional[str] = None, port: int = 22, timeout: int = 8):
        self.username = username
        self.password = password
        self.key_filename = key_filename
        self.port = port
        self.timeout = timeout

    def run_vtysh(self, host: str, command: str) -> DeviceResult:
        """Run a single vtysh command on a Containerlab FRR node."""
        full_cmd = f"vtysh -c {shlex.quote(command)}"
        return self._run(host, full_cmd, display_command=command)

    def run_raw(self, host: str, command: str) -> DeviceResult:
        """Run a raw shell command on the FRR container (e.g. `ip link show eth1`)."""
        return self._run(host, command, display_command=command)

    def _run(self, host: str, shell_command: str, display_command: str) -> DeviceResult:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            connect_kwargs = dict(
                hostname=host,
                port=self.port,
                username=self.username,
                timeout=self.timeout,
            )
            if self.key_filename:
                connect_kwargs["key_filename"] = self.key_filename
            else:
                connect_kwargs["password"] = self.password

            client.connect(**connect_kwargs)
            stdin, stdout, stderr = client.exec_command(shell_command, timeout=self.timeout)
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            exit_status = stdout.channel.recv_exit_status()

            return DeviceResult(
                host=host,
                command=display_command,
                success=(exit_status == 0),
                output=out.strip(),
                error=err.strip() or None,
            )
        except Exception as e:
            return DeviceResult(
                host=host,
                command=display_command,
                success=False,
                output="",
                error=f"{type(e).__name__}: {e}",
            )
        finally:
            client.close()
