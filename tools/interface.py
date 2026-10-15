"""
tool2: Interface
Purpose: send interface state -> runs `show interface` (vtysh) for all
interfaces, or `show interface <name>` for a specific one, and parses
admin/link state plus IP address. (FRR 8.5 has no `show interface detail`;
it treats `detail` as an interface name and prints "% Can't find interface detail".)

FRR prints the two states on the header line:
    Interface eth1 is up, line protocol is down
                      ^^ admin (IFF_UP)    ^^^^ link / carrier (IFF_RUNNING)
A plain "Interface eth1 is down" means the interface is admin down (no line
protocol part is printed then). The old parser read only the first word, so a
link with no carrier (cable out, far end down) parsed as up.
"""

import re

from tools.base_tool import BaseTool, ToolResult

IFACE_HEADER_RE = re.compile(
    r"^Interface (?P<name>\S+) is (?P<admin>up|down)"
    r"(?:, line protocol (?:is (?P<link>up|down)|detection is disabled))?",
    re.MULTILINE,
)
# FRR shows admin state only through the UP flag: `flags: <UP,BROADCAST,...>` vs
# `flags: <BROADCAST,MULTICAST>` after `shutdown`. It never prints "administratively down".
# RUNNING in the same flags line means the link has carrier.
FLAGS_RE = re.compile(r"flags: <(?P<flags>[^>]*)>")
IP_RE = re.compile(r"^\s+inet (?P<ip>\d{1,3}(?:\.\d{1,3}){3})/(?P<prefix>\d+)", re.MULTILINE)
MTU_RE = re.compile(r"\bmtu (?P<mtu>\d+)")
LINK_UPS_RE = re.compile(r"Link ups:\s+(?P<n>\d+)")
LINK_DOWNS_RE = re.compile(r"Link downs:\s+(?P<n>\d+)")


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
            flags_match = FLAGS_RE.search(block)
            flags = flags_match.group("flags").split(",") if flags_match else None

            # admin: the UP flag; no flags line -> unknown (None), never a guess
            admin_state = ("up" if "UP" in flags else "down") if flags is not None else None

            # link: "line protocol is X" when printed; an admin-down interface has no link
            if m.group("link"):
                link_state = m.group("link")
            elif m.group("admin") == "down":
                link_state = "down"
            elif flags is not None:
                link_state = "up" if "RUNNING" in flags else "down"
            else:
                link_state = "up"   # "line protocol detection is disabled": assume carrier

            ips = [f"{ip['ip']}/{ip['prefix']}" for ip in IP_RE.finditer(block)]
            mtu = MTU_RE.search(block)
            ups, downs = LINK_UPS_RE.search(block), LINK_DOWNS_RE.search(block)
            interfaces[name] = {
                "link_state": link_state,
                "admin_state": admin_state,
                "ip_address": ips[0] if ips else None,   # first address, kept for older callers
                "ip_addresses": ips,
                "mtu": int(mtu["mtu"]) if mtu else None,
                "link_ups": int(ups["n"]) if ups else None,
                "link_downs": int(downs["n"]) if downs else None,   # a high count = flapping link
            }
        return {"interfaces": interfaces}
