"""Convert analyzer verdicts into dashboard-ready alerts."""

from __future__ import annotations
from typing import Any, Optional
from alerts.models import Alert, AlertSeverity, AlertType


def verdict_to_alert(verdict, device: str, neighbor: str) -> Optional[Alert]:
    """Return None for healthy verdicts or an Alert for detected faults."""
    def value(name: str, default: Any = None) -> Any:
        if isinstance(verdict, dict):
            return verdict.get(name, default)
        return getattr(verdict, name, default)

    if _is_healthy_verdict(value):
        return None

    root_cause = value("root_cause") or ""
    suggested_fix = value("suggested_fix") or ""

    evidence: dict = {}
    tool_errors: dict[str, str] = {}
    for item in value("evidence", []) or []:
        tool_id = item.get("tool", "")
        parsed = item.get("parsed") or {}
        if item.get("success") is False:
            tool_errors[tool_id] = item.get("error") or "Tool execution failed."
        if tool_id == "bgp_state":
            state = parsed.get("queried_peer_state") or parsed.get("peers", {})
            if isinstance(state, str):
                evidence["bgp_state"] = state
        elif tool_id == "interface":
            ifaces = parsed.get("interfaces", {})
            if ifaces:
                evidence["interfaces"] = {
                    n: {
                        "link_state": i.get("link_state"),
                        "admin_state": i.get("admin_state"),
                        "ip_address": i.get("ip_address"),
                    }
                    for n, i in ifaces.items()
                }
        elif tool_id == "tcp_port":
            evidence["tcp_reachable"] = parsed.get("reachable")
            evidence["tcp_port"] = parsed.get("port", 179)
        elif tool_id == "config":
            if parsed.get("has_baseline"):
                evidence["config_drifted"] = parsed.get("drifted")
    if tool_errors:
        evidence["tool_failures"] = tool_errors

    if tool_errors and not value("resolved", False):
        failed_tools = ", ".join(tool_errors)
        root_cause = f"Telemetry collection failed for: {failed_tools}."
        suggested_fix = "Restore router reachability and SSH access, then rerun the checks."

    root = root_cause.lower()
    if tool_errors and not value("resolved", False):
        failed_tool = next(iter(tool_errors))
        alert_type = {
            "interface": AlertType.INTERFACE,
            "tcp_port": AlertType.TCP,
            "config": AlertType.CONFIG,
        }.get(failed_tool, AlertType.BGP_STATE)
        severity = (
            AlertSeverity.CRITICAL
            if failed_tool in {"bgp_state", "tcp_port"}
            else AlertSeverity.WARNING
        )
    elif "interface" in root and "down" in root:
        alert_type = AlertType.INTERFACE
        severity = AlertSeverity.CRITICAL
    elif "tcp" in root or "port 179" in root or "reachab" in root:
        alert_type = AlertType.TCP
        severity = AlertSeverity.CRITICAL
    elif "config" in root or "drift" in root or "baseline" in root or "mismatch" in root:
        alert_type = AlertType.CONFIG
        severity = AlertSeverity.WARNING
    else:
        alert_type = AlertType.BGP_STATE
        severity = AlertSeverity.WARNING

    current_bgp_state = evidence.get("bgp_state")
    if isinstance(current_bgp_state, dict):
        current_bgp_state = next(iter(current_bgp_state.values()), None)

    return Alert(
        device=device,
        neighbor=neighbor,
        alert_type=alert_type,
        severity=severity,
        current_state=current_bgp_state or ("Unavailable" if tool_errors else None),
        message=root_cause or "BGP fault detected.",
        cause=root_cause or "Unknown cause.",
        recommended_action=suggested_fix or "Review BGP configuration and peer state.",
        evidence=evidence,
    )


def _is_healthy_verdict(value) -> bool:
    if not value("resolved", False) or value("source") != "rules":
        return False

    for item in value("evidence", []) or []:
        if item.get("tool") != "bgp_state":
            continue
        parsed = item.get("parsed") or {}
        state = parsed.get("queried_peer_state")
        if state is None:
            peers = parsed.get("peers") or {}
            state = next(iter(peers.values()), None) if len(peers) == 1 else None
        if state == "Established":
            return True
    return False
