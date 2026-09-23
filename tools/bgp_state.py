"""
BGP Collect tool.

Scope:
- Collect BGP operational output from an FRR device.
- Keep device connectivity in the existing FRRDeviceClient.
- Keep parsing separate from collection.
- Do not modify the REST API, interface, TCP, config, or rules-db layers.
"""

import re
from typing import Optional

from tools.base_tool import BaseTool, ToolResult


class BGPStateTool(BaseTool):
    tool_id = "bgp_state"

    # Commands belonging to the BGP Collect scope.
    COLLECT_COMMANDS = {
        "summary": "show bgp summary",
        "neighbors": "show bgp neighbors",
        "routes": "show bgp ipv4 unicast",
    }

    def run(self, host: str, peer: Optional[str] = None) -> ToolResult:
        """
        Preserve the existing tool/API contract:
        run() collects and parses BGP summary.
        """
        result = self.device_client.run_vtysh(
            host, self.COLLECT_COMMANDS["summary"]
        )
        parsed = self._parse_summary(result.output, peer)
        return self._wrap(host, result, parsed)

    def collect(self, host: str) -> dict:
        """
        Collect all BGP operational data required by the Collect stage.

        Returns raw command output only. Parsing remains a separate concern.
        Each command is executed through the existing device_client.
        """
        collected = {}

        for name, command in self.COLLECT_COMMANDS.items():
            result = self.device_client.run_vtysh(host, command)

            collected[name] = {
                "command": command,
                "success": result.success,
                "raw_output": result.output,
                "error": result.error,
            }

        return collected

    def _parse_summary(self, output: str, peer: Optional[str]) -> dict:
        """
        Parse FRR 'show bgp summary'.

        FRR's State/PfxRcd field is column 9 in the peer row. Current FRR
        versions may print '(Policy)' and additional columns after it.
        """
        peers = {}
        ip_re = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

        for line in output.splitlines():
            fields = line.split()

            if len(fields) < 10 or not ip_re.match(fields[0]):
                continue

            neighbor = fields[0]
            state_field = fields[9]

            if state_field.isdigit() or state_field == "(Policy)":
                state = "Established"
            elif state_field in {
                "Active",
                "Connect",
                "Idle",
                "OpenSent",
                "OpenConfirm",
            }:
                state = state_field
            elif state_field.startswith("Idle"):
                state = "Idle"
            else:
                state = state_field

            peers[neighbor] = state

        parsed = {"peers": peers}

        if peer:
            parsed["queried_peer_state"] = peers.get(peer, "unknown")

        return parsed
