"""
tool5: BGP neighbor
Purpose: runs `show bgp neighbors <peer>` and parses AS numbers, state, admin
shutdown and the last reset / NOTIFICATION. On Bad Peer AS it also reads the
peer's real AS from the OPEN hex dump.
"""

import re

from tools.base_tool import BaseTool, ToolResult

HEADER_RE = re.compile(r"BGP neighbor is (?P<peer>\S+), remote AS (?P<remote_as>\d+), "
                       r"local AS (?P<local_as>\d+), (?P<link>\w+) link")
STATE_RE = re.compile(r"BGP state = (?P<state>\w+)")
HOSTNAME_RE = re.compile(r"^Hostname: (?P<name>\S+)", re.MULTILINE)
ROUTER_ID_RE = re.compile(r"remote router ID (?P<id>[\d.]+)")
CONNECTIONS_RE = re.compile(r"Connections established (?P<up>\d+); dropped (?P<dropped>\d+)")
LAST_RESET_RE = re.compile(r"Last reset \S+,\s+(?P<reason>.+)")
NOTIFICATION_RE = re.compile(r"Notification (?P<direction>sent|received) \((?P<error>[^)]+)\)")
LOCAL_HOST_RE = re.compile(r"^Local host: (?P<ip>[\d.]+)", re.MULTILINE)
HEX_RE = re.compile(r"^\s+(?:[0-9A-F]{2,8}\s*)+$")

AS_TRANS = 23456
CAP_4BYTE_AS = 65


class BGPNeighborTool(BaseTool):
    tool_id = "bgp_neighbor"

    def run(self, host: str, peer: str) -> ToolResult:
        result = self.device_client.run_vtysh(host, f"show bgp neighbors {peer}")
        return self._wrap(host, result, self._parse(result.output))

    def _parse(self, output: str) -> dict:
        header = HEADER_RE.search(output)
        if not header:
            # "% No such neighbor in this view/vrf"
            return {"configured": False}

        parsed = {
            "configured": True,
            "remote_as": int(header["remote_as"]),
            "local_as": int(header["local_as"]),
            "link": header["link"],
            "state": _group(STATE_RE, output, "state"),
            "admin_shutdown": "Administratively shut down" in output,
            "hostname": _group(HOSTNAME_RE, output, "name"),
            "remote_router_id": _group(ROUTER_ID_RE, output, "id"),
            "local_host": _group(LOCAL_HOST_RE, output, "ip"),
            "last_reset": _group(LAST_RESET_RE, output, "reason"),
            "notification": None,
            "peer_open_as": None,
        }
        conns = CONNECTIONS_RE.search(output)
        if conns:
            parsed["connections_established"] = int(conns["up"])
            parsed["connections_dropped"] = int(conns["dropped"])

        note = NOTIFICATION_RE.search(parsed["last_reset"] or "")
        if note:
            parsed["notification"] = {"direction": note["direction"], "error": note["error"]}
            # The hex dump stays after later resets, so only trust it for this error.
            if note["direction"] == "sent" and note["error"].startswith("OPEN Message Error"):
                parsed["peer_open_as"] = _open_as(output)
        return parsed


def _group(regex: re.Pattern, text: str, name: str) -> str | None:
    m = regex.search(text)
    return m[name] if m else None


def _open_as(output: str) -> int | None:
    """My AS from the dumped OPEN (the 4-byte AS capability if it's AS_TRANS)."""
    _, _, dump = output.partition("Message received that caused BGP to send a NOTIFICATION:")
    hex_text = ""
    for line in dump.splitlines()[1:]:
        if not HEX_RE.match(line):
            break
        hex_text += "".join(line.split())
    try:
        msg = bytes.fromhex(hex_text)
    except ValueError:
        return None
    if len(msg) < 29 or msg[18] != 1:   # 16-byte marker, length, type 1 = OPEN
        return None
    asn = int.from_bytes(msg[20:22], "big")
    if asn != AS_TRANS:
        return asn

    params, i = msg[29:29 + msg[28]], 0
    while i + 2 <= len(params):
        ptype, plen = params[i], params[i + 1]
        caps, j = params[i + 2:i + 2 + plen], 0
        while ptype == 2 and j + 2 <= len(caps):
            code, clen = caps[j], caps[j + 1]
            if code == CAP_4BYTE_AS and clen == 4:
                return int.from_bytes(caps[j + 2:j + 6], "big")
            j += 2 + clen
        i += 2 + plen
    return None
