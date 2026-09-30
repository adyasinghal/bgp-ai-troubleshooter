"""
tool2: Interface
Purpose: send interface state -> runs `show interface` (vtysh) for all
interfaces, or `show interface <name>` for a specific one, and parses
admin/link state plus IP address. (FRR 8.5 has no `show interface detail`;
it treats `detail` as an interface name and prints "% Can't find interface detail".)
"""

import re

from tools.base_tool import BaseTool, ToolResult

IFACE_HEADER_RE = re.compile(r"^Interface (?P<name>\S+) is (?P<link>up|down)", re.MULTILINE)
# FRR shows admin state only through the UP flag: `flags: <UP,BROADCAST,...>` vs
# `flags: <BROADCAST,MULTICAST>` after `shutdown`. It never prints "administratively down".
FLAGS_RE = re.compile(r"flags: <(?P<flags>[^>]*)>")
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
            flags_match = FLAGS_RE.search(block)
            if flags_match:
                admin_state = "up" if "UP" in flags_match.group("flags").split(",") else "down"
            else:
                admin_state = None   # no flags line, so admin state is unknown
            ip_match = IP_RE.search(block)
            interfaces[name] = {
                "link_state": link_state,
                "admin_state": admin_state,
                "ip_address": f"{ip_match.group('ip')}/{ip_match.group('prefix')}" if ip_match else None,
            }
        return {"interfaces": interfaces}
