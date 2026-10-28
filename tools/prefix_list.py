"""
tool9: Prefix list
Purpose: runs `show ip prefix-list [name]` and reports every entry, so a
route-map's `match ip address prefix-list X` (or a neighbor's `prefix-list X in`)
can be read, not just its name. Pass `prefix` to also evaluate it against each
list the way FRR does: the first matching entry decides, and a prefix no entry
matches is denied (the implicit deny at the end of every list).

FRR 8.5 output (each daemon prints its own copy; the BGP one is used):
    BGP: ip prefix-list BLOCK: 2 entries
       seq 5 deny 172.16.2.0/24
       seq 10 permit 0.0.0.0/0 le 32
"""

import ipaddress
import re

from tools.base_tool import BaseTool, ToolResult

LIST_RE = re.compile(r"^(?:(?P<daemon>[A-Z]+): )?ip prefix-list (?P<name>\S+): \d+ entries")
ENTRY_RE = re.compile(r"^\s+seq (?P<seq>\d+) (?P<action>permit|deny) (?P<prefix>any|\S+/\d+)"
                      r"(?: ge (?P<ge>\d+))?(?: le (?P<le>\d+))?")


class PrefixListTool(BaseTool):
    tool_id = "prefix_list"

    def run(self, host: str, name: str | None = None, prefix: str | None = None) -> ToolResult:
        command = f"show ip prefix-list {name}" if name else "show ip prefix-list"
        result = self.device_client.run_vtysh(host, command)
        return self._wrap(host, result, self._parse(result.output, prefix))

    def _parse(self, output: str, prefix: str | None = None) -> dict:
        lists, current, use_daemon = {}, None, None
        for line in (output or "").splitlines():
            m = LIST_RE.match(line)
            if m:
                daemon = m["daemon"]
                # keep the BGP daemon's copy when daemons are listed separately
                if use_daemon is None:
                    use_daemon = "BGP" if "BGP: ip prefix-list" in output else daemon
                current = None
                if daemon == use_daemon:
                    current = lists.setdefault(m["name"], {"name": m["name"], "entries": []})
                continue
            m = ENTRY_RE.match(line)
            if m and current is not None:
                current["entries"].append({
                    "sequence": int(m["seq"]),
                    "action": m["action"],
                    "prefix": "0.0.0.0/0" if m["prefix"] == "any" else m["prefix"],
                    "any": m["prefix"] == "any",
                    "ge": int(m["ge"]) if m["ge"] else None,
                    "le": int(m["le"]) if m["le"] else None,
                })
        parsed = {"prefix_lists": list(lists.values()), "count": len(lists)}
        if prefix:
            parsed["prefix"] = prefix
            try:
                parsed["evaluation"] = {name: evaluate(pl["entries"], prefix) for name, pl in lists.items()}
            except ValueError as e:
                parsed["evaluation"] = None
                parsed["evaluation_error"] = str(e)
        return parsed


def matches(entry: dict, prefix: str) -> bool:
    """FRR prefix-list matching: inside the entry's network, and a length that is
    exactly the entry's length unless ge/le widen the range."""
    route = ipaddress.ip_network(prefix, strict=False)
    if entry.get("any"):
        return True
    net = ipaddress.ip_network(entry["prefix"], strict=False)
    if route.version != net.version or not route.subnet_of(net):
        return False
    low = entry["ge"] if entry["ge"] is not None else net.prefixlen
    high = entry["le"] if entry["le"] is not None else (route.max_prefixlen if entry["ge"] is not None
                                                        else net.prefixlen)
    return low <= route.prefixlen <= high


def evaluate(entries: list[dict], prefix: str) -> dict:
    """{action, sequence}: the first entry that matches, or the implicit deny."""
    for e in sorted(entries, key=lambda e: e["sequence"]):
        if matches(e, prefix):
            return {"action": e["action"], "sequence": e["sequence"], "implicit": False}
    return {"action": "deny", "sequence": None, "implicit": True}
