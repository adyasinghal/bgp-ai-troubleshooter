"""
tool8: Route to peer
Purpose: runs `show ip route <peer>` to check that this router can reach the
peer's address at all, and how. BGP can't open TCP to a peer it has no route
to, and a route that only matches the default (often the management network
in Containerlab) does not lead to the peer either.

FRR 8.5 output:
    Routing entry for 10.0.12.0/30
      Known via "connected", distance 0, metric 0, best
      Last update 00:10:12 ago
      * directly connected, eth1
or "% Network not in table".
"""

import ipaddress
import re

from tools.base_tool import BaseTool, ToolResult
from tools.device_client import DeviceResult

ENTRY_RE = re.compile(r"^Routing entry for (?P<prefix>\S+)", re.MULTILINE)
KNOWN_VIA_RE = re.compile(r'Known via "(?P<proto>[^"]+)"')
CONNECTED_RE = re.compile(r"^\s+\*?\s*directly connected, (?P<iface>\S+)", re.MULTILINE)
VIA_RE = re.compile(r"^\s+\*?\s*(?P<nexthop>[\d.]+)(?:, via (?P<iface>\S+))?", re.MULTILINE)


class RouteToPeerTool(BaseTool):
    tool_id = "route_to_peer"

    def run(self, host: str, peer: str) -> ToolResult:
        try:
            peer = str(ipaddress.ip_address(peer))
        except ValueError:
            bad = DeviceResult(host=host, command="show ip route <peer>", success=False, output="",
                               error=f"peer {peer!r} is not an IP address")
            return self._wrap(host, bad, {"peer": peer, "has_route": None})
        cmd = f"show ipv6 route {peer}" if ":" in peer else f"show ip route {peer}"
        result = self.device_client.run_vtysh(host, cmd)
        return self._wrap(host, result, self._parse(result.output, peer))

    def _parse(self, output: str, peer: str) -> dict:
        entry = ENTRY_RE.search(output or "")
        if not entry:
            # "% Network not in table"
            return {"peer": peer, "has_route": False, "prefix": None, "protocol": None,
                    "is_default": False, "connected": False, "next_hop": None, "interface": None}
        prefix = entry["prefix"]
        proto = KNOWN_VIA_RE.search(output)
        connected = CONNECTED_RE.search(output)
        via = None if connected else VIA_RE.search(output[entry.end():])
        try:
            is_default = ipaddress.ip_network(prefix, strict=False).prefixlen == 0
        except ValueError:
            is_default = False
        return {
            "peer": peer,
            "has_route": True,
            "prefix": prefix,
            "protocol": proto["proto"] if proto else None,
            "is_default": is_default,          # only the default route matches: no real route to the peer
            "connected": connected is not None,
            "next_hop": via["nexthop"] if via else None,
            "interface": connected["iface"] if connected else (via["iface"] if via else None),
        }
