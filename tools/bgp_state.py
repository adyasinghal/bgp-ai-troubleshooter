"""
BGP Collect tool.

Scope:
- Collect BGP operational output from an FRR device.
- Keep device connectivity in the existing FRRDeviceClient.
- Keep parsing separate from collection.
- Do not modify the REST API, interface, TCP, config, or rules-db layers.
"""

import json
import re
from typing import Optional

from tools.base_tool import BaseTool, ToolResult


class BGPStateTool(BaseTool):
    tool_id = "bgp_state"

    # Commands belonging to the BGP Collect scope.
    COLLECT_COMMANDS = {
        "summary": "show bgp summary",
        "neighbors": "show bgp neighbors json",
        "routes": "show bgp ipv4 unicast json",
    }

    def run(self, host: str, peer: Optional[str] = None) -> ToolResult:
        """
        Preserve the existing tool/API contract:
        run() collects and parses BGP summary.
        """
        result = self.device_client.run_vtysh(
            host, self.COLLECT_COMMANDS["summary"]
        )
        parsed = self._parse_summary(result.output, peer)
        return self._wrap(host, result, parsed)

    def collect(self, host: str) -> dict:
        """
        Collect all BGP operational data required by the Collect stage.

        Returns raw command output and structured parsed telemetry.
        """
        collected = {}

        for name, command in self.COLLECT_COMMANDS.items():
            result = self.device_client.run_vtysh(host, command)

            parsed = {}
            if result.success and result.output:
                if name == "summary":
                    parsed = self._parse_summary(result.output, peer=None)
                elif name == "neighbors":
                    parsed = self._parse_neighbors_json(result.output)
                elif name == "routes":
                    parsed = self._parse_routes_json(result.output)

            collected[name] = {
                "command": command,
                "success": result.success,
                "raw_output": result.output,
                "parsed": parsed,
                "error": result.error,
            }

        return collected

    def _parse_summary(self, output: str, peer: Optional[str] = None) -> dict:
        """
        Parse FRR 'show bgp summary' supporting both text table and JSON formats.
        """
        if not output or not isinstance(output, str):
            parsed = {"peers": {}}
            if peer:
                parsed["queried_peer_state"] = "unknown"
            return parsed

        clean_out = output.strip()
        if clean_out.startswith("{"):
            try:
                raw_json = json.loads(clean_out)
                ipv4 = raw_json.get("ipv4Unicast", raw_json) if isinstance(raw_json, dict) else {}
                raw_peers = ipv4.get("peers", {}) if isinstance(ipv4, dict) else {}
                peers = {}
                if isinstance(raw_peers, dict):
                    for p_ip, p_data in raw_peers.items():
                        if isinstance(p_data, dict):
                            peers[p_ip] = p_data.get("state", "unknown")
                parsed = {"peers": peers}
                if peer:
                    parsed["queried_peer_state"] = peers.get(peer, "unknown")
                return parsed
            except Exception:
                pass

        # Text table parsing fallback
        peers = {}
        ip_re = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

        for line in output.splitlines():
            fields = line.split()

            if len(fields) < 10 or not ip_re.match(fields[0]):
                continue

            neighbor = fields[0]
            state_field = fields[9]

            if state_field.isdigit() or state_field == "(Policy)":
                state = "Established"
            elif state_field in {
                "Active",
                "Connect",
                "Idle",
                "OpenSent",
                "OpenConfirm",
            }:
                state = state_field
            elif state_field.startswith("Idle"):
                state = "Idle"
            else:
                state = state_field

            peers[neighbor] = state

        parsed = {"peers": peers}

        if peer:
            parsed["queried_peer_state"] = peers.get(peer, "unknown")

        return parsed

    def _parse_neighbors_json(self, output: str) -> dict:
        """
        Parse FRR 'show bgp neighbors json' into structured telemetry.
        """
        if not output or not isinstance(output, str):
            return {"neighbors": {}}

        try:
            raw_data = json.loads(output)
        except Exception:
            return {"neighbors": {}}

        if not isinstance(raw_data, dict):
            return {"neighbors": {}}

        neighbors = {}
        for peer_ip, data in raw_data.items():
            if not isinstance(data, dict):
                continue

            msg_stats = data.get("messageStats") if isinstance(data.get("messageStats"), dict) else {}
            af_info = data.get("addressFamilyInfo") if isinstance(data.get("addressFamilyInfo"), dict) else {}
            ipv4_af = af_info.get("ipv4Unicast") if isinstance(af_info.get("ipv4Unicast"), dict) else {}

            hold_time = data.get("holdTime")
            if hold_time is None and data.get("holdTimeMsecs") is not None:
                try:
                    hold_time = int(data["holdTimeMsecs"]) // 1000
                except (ValueError, TypeError):
                    hold_time = None

            keepalive = data.get("keepAliveTime")
            if keepalive is None and data.get("keepAliveTimeMsecs") is not None:
                try:
                    keepalive = int(data["keepAliveTimeMsecs"]) // 1000
                except (ValueError, TypeError):
                    keepalive = None

            notifications_sent = data.get("notificationsSent")
            if notifications_sent is None:
                notifications_sent = msg_stats.get("notificationsSent")

            notifications_received = data.get("notificationsReceived")
            if notifications_received is None:
                notifications_received = msg_stats.get("notificationsReceived")

            neighbors[peer_ip] = {
                "peer": peer_ip,
                "remote_router_id": data.get("remoteRouterId"),
                "local_router_id": data.get("localRouterId"),
                "remote_as": data.get("remoteAs"),
                "local_as": data.get("localAs"),
                "bgp_state": data.get("bgpState"),
                "hold_time": hold_time,
                "keepalive_time": keepalive,
                "uptime": data.get("bgpTimerUpString"),
                "last_reset_due_to": data.get("lastResetDueTo"),
                "last_reset_code": data.get("lastResetCode"),
                "last_reset_subcode": data.get("lastResetSubcode"),
                "notifications_sent": notifications_sent,
                "notifications_received": notifications_received,
                "prefix_received_count": ipv4_af.get("prefixReceivedCount"),
                "prefix_advertised_count": ipv4_af.get("prefixAdvertisedCount"),
            }

        return {"neighbors": neighbors}

    def _parse_routes_json(self, output: str) -> dict:
        """
        Parse FRR 'show bgp ipv4 unicast json' into structured routing telemetry.
        """
        if not output or not isinstance(output, str):
            return {"routes": {}}

        try:
            raw_data = json.loads(output)
        except Exception:
            return {"routes": {}}

        if not isinstance(raw_data, dict):
            return {"routes": {}}

        router_id = raw_data.get("routerId")
        local_as = raw_data.get("localAS") or raw_data.get("as")
        raw_routes = raw_data.get("routes") if isinstance(raw_data.get("routes"), dict) else {}

        parsed_routes = {}
        for prefix, paths in raw_routes.items():
            if not isinstance(paths, list):
                continue

            parsed_paths = []
            for path in paths:
                if not isinstance(path, dict):
                    continue

                aspath_data = path.get("aspath")
                as_path_str = aspath_data.get("string") if isinstance(aspath_data, dict) else (aspath_data if isinstance(aspath_data, str) else None)
                as_path_segments = aspath_data.get("segments", []) if isinstance(aspath_data, dict) else []

                raw_nexthops = path.get("nexthops")
                next_hops = []
                if isinstance(raw_nexthops, list):
                    for nh in raw_nexthops:
                        if isinstance(nh, dict) and "ip" in nh:
                            next_hops.append(nh["ip"])
                        elif isinstance(nh, str):
                            next_hops.append(nh)

                parsed_paths.append({
                    "prefix": path.get("prefix", prefix),
                    "valid": path.get("valid"),
                    "bestpath": path.get("bestpath"),
                    "multipath": path.get("multipath"),
                    "path_from": path.get("pathFrom"),
                    "peer": path.get("peer"),
                    "origin": path.get("origin"),
                    "metric": path.get("metric"),
                    "loc_prf": path.get("locPrf"),
                    "weight": path.get("weight"),
                    "as_path": as_path_str,
                    "as_path_segments": as_path_segments,
                    "next_hops": next_hops,
                })

            parsed_routes[prefix] = parsed_paths

        result = {"routes": parsed_routes}
        if router_id is not None:
            result["router_id"] = router_id
        if local_as is not None:
            result["local_as"] = local_as
        return result

    # Backward compatibility alias
    _parse = _parse_summary

