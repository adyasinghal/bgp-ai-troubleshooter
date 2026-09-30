"""
tool3: TCP / port
Purpose: check TCP reachability on BGP's well-known port 179 between the
local FRR node and a peer. Uses a raw shell command inside the container
(bash /dev/tcp trick, no extra packages needed) rather than vtysh, since this
is a transport-layer check, not a BGP-layer one.
"""

from tools.base_tool import BaseTool, ToolResult

DEFAULT_BGP_PORT = 179


class TCPPortTool(BaseTool):
    tool_id = "tcp_port"

    def run(self, host: str, peer_ip: str, port: int = DEFAULT_BGP_PORT) -> ToolResult:
        # /dev/tcp is a bash builtin available in most FRR container base images
        command = (
            f'timeout 3 bash -c "echo > /dev/tcp/{peer_ip}/{port}" '
            f'&& echo REACHABLE || echo UNREACHABLE'
        )
        result = self.device_client.run_raw(host, command)
        # Exact match: "UNREACHABLE" contains "REACHABLE", so a substring test
        # reported every blocked port as reachable.
        reachable = result.output.strip() == "REACHABLE"
        parsed = {
            "peer_ip": peer_ip,
            "port": port,
            "reachable": reachable,
        }
        return self._wrap(host, result, parsed)
