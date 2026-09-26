"""
tool1: BGP state
Purpose: send bgp state -> runs `show bgp summary` on the FRR node via vtysh
and parses out each peer's state (Established, Active, Connect, Idle...).
"""

import re

from tools.base_tool import BaseTool, ToolResult

"""
# vtysh "show bgp summary" prints a table like:
# Neighbor        V         AS   MsgRcvd   MsgSent ... State/PfxRcd
# 10.0.1.1        4      65001         0         0 ...          Active
PEER_LINE_RE = re.compile(
    r"^(?P<neighbor>\d{1,3}(?:\.\d{1,3}){3})\s+\d+\s+\d+.*?\s(?P<state>Established|Active|Connect|Idle|OpenSent|OpenConfirm|\d+)\s*$",
    re.MULTILINE,
)
"""

# FIX:
# Before: the regex PEER_LINE_RE assumed the peer line ENDS with the state, so
# on current FRR output it matched nothing and returned {} -> "queried_peer_state"
# came back "unknown" even though `raw_output` clearly showed an Established peer.
# Cause: newer FRR prints "(Policy)" in the state column (eBGP up but policy-
# filtered) plus extra trailing columns (PfxSnt, Desc), which the regex rejected.
# Fix: split each line into columns and read column 9 (State/PfxRcd) by index --
# stable no matter how many extra columns follow -> parsed peers now populate.

class BGPStateTool(BaseTool):
    tool_id = "bgp_state"

    def run(self, host: str, peer: str | None = None) -> ToolResult:
        result = self.device_client.run_vtysh(host, "show bgp summary")
        parsed = self._parse(result.output, peer)
        return self._wrap(host, result, parsed)

    def _parse(self, output: str, peer: str | None) -> dict:
        # FRR "show bgp summary" prints a fixed-order table:
        #   Neighbor V AS MsgRcvd MsgSent TblVer InQ OutQ Up/Down State/PfxRcd [PfxSnt] [Desc]
        #      0     1  2    3       4      5     6   7      8          9          10      11
        # Column 9 (State/PfxRcd) carries the state and stays at the same index
        # no matter how many extra columns a given FRR version prints after it.
        peers = {}
        ip_re = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

        for line in output.splitlines():
            fields = line.split()
            # a real peer line starts with an IP and has at least 10 columns
            if len(fields) < 10 or not ip_re.match(fields[0]):
                continue

            neighbor = fields[0]
            state_field = fields[9]          # the State/PfxRcd column

            if state_field.isdigit() or state_field == "(Policy)":
                # a number = prefixes received; "(Policy)" = up but policy-filtered
                state = "Established"
            elif state_field in {"Active", "Connect", "Idle", "OpenSent", "OpenConfirm"}:
                state = state_field
            elif state_field.startswith("Idle"):   # e.g. "Idle (Admin)"
                state = "Idle"
            else:
                state = state_field           # unknown -> pass through as-is

            peers[neighbor] = state

        parsed = {"peers": peers}
        if peer:
            parsed["queried_peer_state"] = peers.get(peer, "unknown")
        return parsed
