"""
tool2: Interface
Purpose: send interface state -> runs `show interface detail` (vtysh) or
falls back to `show interface <name>` for a specific interface, and parses
admin/link state plus IP address.
"""

import re

from tools.base_tool import BaseTool, ToolResult

IFACE_HEADER_RE = re.compile(r"^Interface (?P<name>\S+) is (?P<link>up|down)", re.MULTILINE)
ADMIN_RE = re.compile(r"administratively (?P<admin>up|down)")
IP_RE = re.compile(r"inet (?P<ip>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d+)")


class InterfaceTool(BaseTool):
    tool_id = "interface"

    def run(self, host: str, interface: str | None = None) -> ToolResult:
        command = f"show interface {interface}" if interface else "show interface"
        result = self.device_client.run_vtysh(host, command)
        parsed = self._parse(result.output)
        return self._wrap(host, result, parsed)

    def _parse(self, output: str) -> dict:
        interfaces = {}
        blocks = re.split(r"\n(?=Interface \S+ is )", output)
        for block in blocks:
            m = IFACE_HEADER_RE.search(block)
            if not m:
                continue
            name = m.group("name")
            link_state = m.group("link")
            admin_match = ADMIN_RE.search(block)
            admin_state = admin_match.group("admin") if admin_match else "up"
            ip_match = IP_RE.search(block)
            interfaces[name] = {
                "link_state": link_state,
                "admin_state": admin_state,
                "ip_address": f"{ip_match.group('ip')}/{ip_match.group('prefix')}" if ip_match else None,
            }
        return {"interfaces": interfaces}
