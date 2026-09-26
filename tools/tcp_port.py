"""
tool3: TCP / port
Purpose: check TCP reachability on BGP's well-known port 179 between the
local FRR node and a peer. Uses a raw shell command inside the container
(bash /dev/tcp trick, no extra packages needed) rather than vtysh, since this
is a transport-layer check, not a BGP-layer one.
"""

from ipaddress import IPv4Address

from tools.base_tool import BaseTool, ToolResult

DEFAULT_BGP_PORT = 179


class TCPPortTool(BaseTool):
    tool_id = "tcp_port"

    def run(self, host: str, peer_ip: str, port: int = DEFAULT_BGP_PORT) -> ToolResult:
        try:
            peer_ip = str(IPv4Address(peer_ip))
        except (ValueError, TypeError) as error:
            raise ValueError(f"peer_ip must be a valid IPv4 address: {peer_ip!r}") from error
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port must be an integer between 1 and 65535")

        # /dev/tcp is a bash builtin available in most FRR container base images
        command = (
            f'timeout 3 bash -c "echo > /dev/tcp/{peer_ip}/{port}" '
            f'&& echo REACHABLE || echo UNREACHABLE'
        )
        result = self.device_client.run_raw(host, command)
        reachable = result.output.strip() == "REACHABLE"
        parsed = {
            "peer_ip": peer_ip,
            "port": port,
            "reachable": reachable,
        }
        return self._wrap(host, result, parsed)
