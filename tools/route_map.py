"""
tool7: Route map
Purpose: runs `show route-map [name]` to find policy that filters or changes a
route. Reports each route-map entry's action (permit/deny), sequence, how many
times it matched ("Invoked"), and its match and set clauses.
"""

import re

from tools.base_tool import BaseTool, ToolResult

# FRR 8.5:  route-map: FROM-R2 Invoked: 4 ...
#            deny, sequence 10 Invoked 2
# older:    route-map FROM-R2, deny, sequence 10
MAP_RE = re.compile(r"^route-map: (?P<name>\S+)")
ENTRY_RE = re.compile(r"^\s+(?P<action>permit|deny), sequence (?P<seq>\d+)(?: Invoked (?P<invoked>\d+))?")
OLD_ENTRY_RE = re.compile(r"^route-map (?P<name>\S+), (?P<action>permit|deny), sequence (?P<seq>\d+)")
SECTIONS = {"Match clauses:": "match_clauses", "Set clauses:": "set_clauses",
            "Call clause:": None, "Action:": None}


class RouteMapTool(BaseTool):
    tool_id = "route_map"

    def run(self, host: str, name: str | None = None) -> ToolResult:
        command = f"show route-map {name}" if name else "show route-map"
        result = self.device_client.run_vtysh(host, command)
        parsed = self._parse(result.output)
        return self._wrap(host, result, parsed)

    def _parse(self, output: str) -> dict:
        # FRR lists each route-map under ZEBRA: and again under BGP:; the BGP
        # copy is the one applied to BGP routes and has real Invoked counts.
        _, bgp, bgp_part = output.partition("\nBGP:\n")
        text = bgp_part if bgp else output

        route_maps, name, entry, section = [], None, None, None
        for line in text.splitlines():
            m = MAP_RE.match(line)
            if m:
                name = m["name"]
                continue
            m = ENTRY_RE.match(line) or OLD_ENTRY_RE.match(line)
            if m:
                entry = {
                    "name": m.groupdict().get("name") or name,
                    "action": m["action"],
                    "sequence": int(m["seq"]),
                    "invoked": int(m["invoked"]) if m.groupdict().get("invoked") else None,
                    "match_clauses": [],
                    "set_clauses": [],
                }
                route_maps.append(entry)
                section = None
                continue
            stripped = line.strip()
            if stripped in SECTIONS:
                section = SECTIONS[stripped]
            elif entry and section and stripped:
                entry[section].append(stripped)
        return {"route_maps": route_maps, "count": len(route_maps)}
