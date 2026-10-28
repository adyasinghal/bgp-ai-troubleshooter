"""
tool3: TCP / port
Purpose: check TCP reachability on BGP's well-known port 179 between the
local FRR node and a peer. Uses a raw shell command inside the container
(bash /dev/tcp trick, no extra packages needed) rather than vtysh, since this
is a transport-layer check, not a BGP-layer one.

Besides reachable true/false it reports WHY a connection failed, because the
causes need different fixes:
    refused   - the peer answered with a reset: nothing listens on 179 (bgpd down)
                or a firewall REJECTs it
    timeout   - no answer at all: a firewall DROPs it, or the link loses packets
    no_route  - this router has no route to the peer (or ARP to it fails)
    error     - anything else; see raw_output
"""

import ipaddress
import re

from tools.base_tool import BaseTool, ToolResult
from tools.device_client import DeviceResult

DEFAULT_BGP_PORT = 179
CONNECT_TIMEOUT = 3
# GNU timeout exits 124 when it kills the command; busybox (Alpine FRR image) 143 / 137
TIMEOUT_CODES = {124, 137, 143}
RC_RE = re.compile(r"^RC=(?P<rc>\d+)\s*$", re.MULTILINE)


class TCPPortTool(BaseTool):
    tool_id = "tcp_port"

    def run(self, host: str, peer_ip: str, port: int = DEFAULT_BGP_PORT) -> ToolResult:
        # peer_ip goes into a shell command, so only accept a literal address
        try:
            peer_ip = str(ipaddress.ip_address(peer_ip))
        except ValueError:
            bad = DeviceResult(host=host, command="check port 179", success=False, output="",
                               error=f"peer_ip {peer_ip!r} is not an IP address")
            return self._wrap(host, bad, {"peer_ip": peer_ip, "port": port, "reachable": None,
                                          "reason": "error"})
        port = int(port)
        # /dev/tcp is a bash builtin available in most FRR container base images.
        # 2>&1 keeps bash's error text ("Connection refused"...); RC= is its exit code.
        command = (
            f"timeout {CONNECT_TIMEOUT} bash -c 'echo > /dev/tcp/{peer_ip}/{port}' 2>&1; "
            f'echo "RC=$?"'
        )
        result = self.device_client.run_raw(host, command)
        parsed = {"peer_ip": peer_ip, "port": port, **self._parse(result.output)}
        return self._wrap(host, result, parsed)

    def _parse(self, output: str) -> dict:
        text = output or ""
        m = RC_RE.search(text)
        if m is None:
            # older command / mock devices print REACHABLE or UNREACHABLE
            # (exact match: "UNREACHABLE" contains "REACHABLE")
            reachable = text.strip() == "REACHABLE"
            return {"reachable": reachable, "reason": None if reachable else "unknown"}

        rc = int(m["rc"])
        low = text.lower()
        if rc == 0:
            return {"reachable": True, "reason": None}
        if "refused" in low:
            reason = "refused"
        elif "no route to host" in low or "network is unreachable" in low:
            reason = "no_route"
        elif rc in TIMEOUT_CODES or "timed out" in low:
            reason = "timeout"
        else:
            reason = "error"
        return {"reachable": False, "reason": reason, "exit_code": rc}
