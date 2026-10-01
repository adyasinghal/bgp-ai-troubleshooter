"""
tool6: Route table
Purpose: runs `show ip bgp` (whole BGP table) or `show ip bgp <prefix>` (one
prefix in detail) and reports which paths exist, which one is best (">") and
the attributes the best-path algorithm compares: weight, local-pref, AS-path
length, origin, MED. For one prefix it also gives FRR's reason the best path
won, e.g. "Local Pref" or "AS Path".
"""

import re

from tools.base_tool import BaseTool, ToolResult

HEADER_RE = re.compile(r"^[ \t]*Network\s+Next Hop\s+Metric\s+LocPrf\s+Weight\s+Path", re.MULTILINE)
ORIGINS = {"i": "i", "e": "e", "?": "?", "IGP": "i", "EGP": "e", "incomplete": "?"}

# `show ip bgp <prefix>` detail
ENTRY_RE = re.compile(r"BGP routing table entry for (?P<network>\S+?),")
FROM_RE = re.compile(r"^\s+(?P<nexthop>\S+) from (?P<peer>\S+) \((?P<router_id>[\d.]+)\)")
ATTR_RE = {
    "metric": re.compile(r"metric (\d+)"),
    "local_pref": re.compile(r"localpref (\d+)"),
    "weight": re.compile(r"weight (\d+)"),
}
ORIGIN_RE = re.compile(r"Origin (IGP|EGP|incomplete)")
BEST_RE = re.compile(r"best \((?P<reason>[^)]+)\)")


class RouteTableTool(BaseTool):
    tool_id = "route_table"

    def run(self, host: str, prefix: str | None = None) -> ToolResult:
        command = f"show ip bgp {prefix}" if prefix else "show ip bgp"
        result = self.device_client.run_vtysh(host, command)
        parsed = self._parse(result.output, prefix)
        return self._wrap(host, result, parsed)

    def _parse(self, output: str, prefix: str | None = None) -> dict:
        if "Network not in table" in output:
            paths, in_table = [], False
        elif ENTRY_RE.search(output):
            paths, in_table = _parse_detail(output), True
        else:
            paths, in_table = _parse_table(output), None

        best = next((p for p in paths if p["is_best"]), None)
        parsed = {
            "paths": paths,
            "best_path": best,
            "paths_not_selected": [p for p in paths if not p["is_best"]],
            "path_count": len(paths),
        }
        if prefix:
            parsed["prefix"] = prefix
            parsed["in_table"] = in_table if in_table is not None else bool(paths)
        return parsed


def _path(network, next_hop, metric, local_pref, weight, as_path, origin, is_best, **extra) -> dict:
    return {
        "network": network, "next_hop": next_hop,
        "metric": metric, "local_pref": local_pref, "weight": weight,
        "as_path": as_path, "as_path_length": len(as_path),
        "origin": origin, "is_best": is_best, **extra,
    }


def _number(text: str) -> int | None:
    text = text.strip()
    return int(text) if text.isdigit() else None


def _parse_table(output: str) -> list[dict]:
    """The `show ip bgp` table. Columns are read by position because FRR leaves
    LocPrf (and the network, on a second path to the same prefix) blank."""
    header = HEADER_RE.search(output)
    if not header:
        return []
    line = header.group(0)
    net_at = line.index("Network")
    hop_at = line.index("Next Hop")
    metric_end = line.index("Metric") + len("Metric")
    locprf_end = line.index("LocPrf") + len("LocPrf")
    weight_end = line.index("Weight") + len("Weight")

    paths, network = [], None
    for row in output[header.end():].splitlines():
        status = row[:net_at]
        if "*" not in status:
            continue
        network = row[net_at:hop_at].strip() or network
        hop_metric = row[hop_at:metric_end].split()
        rest = row[weight_end:].split()
        if not hop_metric or not rest:
            continue
        origin = ORIGINS.get(rest[-1], rest[-1])
        paths.append(_path(
            network, hop_metric[0],
            _number(hop_metric[1]) if len(hop_metric) > 1 else None,
            _number(row[metric_end:locprf_end]),
            _number(row[locprf_end:weight_end]),
            rest[:-1], origin, ">" in status,
        ))
    return paths


def _parse_detail(output: str) -> list[dict]:
    """`show ip bgp <prefix>`: an AS-path line, then `<nexthop> from <peer>`,
    then the attributes line (with `best (<reason>)` on the winner)."""
    network = ENTRY_RE.search(output).group("network")
    lines = output.splitlines()
    paths = []
    for i, line in enumerate(lines):
        m = FROM_RE.match(line)
        if not m or i == 0 or i + 1 >= len(lines):
            continue
        as_line = lines[i - 1].strip()
        attrs = lines[i + 1]
        values = {k: (int(r.search(attrs).group(1)) if r.search(attrs) else None) for k, r in ATTR_RE.items()}
        origin = ORIGIN_RE.search(attrs)
        best = BEST_RE.search(attrs)
        paths.append(_path(
            network, m["nexthop"], values["metric"], values["local_pref"], values["weight"],
            [] if as_line == "Local" else as_line.split(),
            ORIGINS.get(origin.group(1)) if origin else None,
            best is not None,
            from_peer=m["peer"], best_reason=best["reason"] if best else None,
        ))
    return paths
