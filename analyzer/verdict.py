"""Verdict: the result the Analyze stage hands to Diagnose/Alert."""
from dataclasses import dataclass, field
from typing import Any

TOOL_DISPLAY = {
    "bgp_state": "BGP state",
    "interface": "Interface state",
    "tcp_port": "TCP port 179",
    "config": "Configuration drift",
}


def _summarize_evidence(tool: str, parsed: dict | None) -> str:
    if not parsed or not isinstance(parsed, dict):
        return "No parsed output available"

    if tool == "bgp_state":
        queried = parsed.get("queried_peer_state")
        peers = parsed.get("peers")
        if queried:
            return f"Peer state: {queried}"
        if peers:
            peers_str = ", ".join(f"{k}: {v}" for k, v in peers.items())
            return f"Peers: {peers_str}"
        return "No peers found"

    if tool == "interface":
        interfaces = parsed.get("interfaces", {})
        if not interfaces:
            return "No interface data"
        parts = []
        for name, info in interfaces.items():
            link = info.get("link_state", "unknown")
            admin = info.get("admin_state", "unknown")
            ip = info.get("ip_address")
            ip_str = f", ip: {ip}" if ip else ""
            parts.append(f"{name} (link: {link}, admin: {admin}{ip_str})")
        return "; ".join(parts)

    if tool == "tcp_port":
        peer_ip = parsed.get("peer_ip", "peer")
        port = parsed.get("port", 179)
        reachable = parsed.get("reachable")
        state = "reachable" if reachable is True else "unreachable" if reachable is False else "unknown"
        return f"Port {port} to {peer_ip} is {state}"

    if tool == "config":
        has_baseline = parsed.get("has_baseline")
        drifted = parsed.get("drifted")
        diff = parsed.get("diff", [])
        if not has_baseline:
            return "No baseline found (diff skipped)"
        if drifted:
            return f"Config drift detected ({len(diff)} diff lines)"
        return "Config matches baseline (no drift)"

    return str(parsed)


@dataclass
class Verdict:
    resolved: bool
    root_cause: str | None = None
    suggested_fix: str | None = None
    checked: list[str] = field(default_factory=list)    # tools tried, in order
    evidence: list[dict[str, Any]] = field(default_factory=list)  # each tool's parsed output
    llm_diagnosis: Any | None = None  # Optional[LLMDiagnosis]

    def pretty(self) -> str:
        lines = []
        is_healthy = self.resolved and (
            "Established" in (self.root_cause or "")
            or "No action needed" in (self.suggested_fix or "")
        )

        if not self.resolved:
            diag = "NO ROOT CAUSE FOUND"
        elif is_healthy:
            diag = "HEALTHY (NO FAULT DETECTED)"
        else:
            diag = "ROOT CAUSE FOUND"

        lines.append(f"Diagnosis: {diag}")
        lines.append("")

        if is_healthy:
            lines.append(f"Status:             {self.root_cause or 'Session is healthy'}")
            lines.append(f"Recommended Action: {self.suggested_fix or 'None'}")
        elif self.resolved:
            lines.append(f"Root Cause:         {self.root_cause or 'Unknown'}")
            lines.append(f"Recommended Action: {self.suggested_fix or 'None'}")
        else:
            lines.append(f"Summary:            {self.root_cause or 'No root cause found'}")
            lines.append(f"Recommended Step:   {self.suggested_fix or 'Escalate'}")

        lines.append("")
        lines.append("Checks Performed:")
        if not self.checked:
            lines.append("  (None)")
        else:
            for idx, tool_id in enumerate(self.checked):
                display = TOOL_DISPLAY.get(tool_id, tool_id)
                if self.resolved and not is_healthy and idx == len(self.checked) - 1:
                    icon = "✗"
                else:
                    icon = "✓"
                lines.append(f"  {icon} {display}")

        if self.evidence:
            lines.append("")
            lines.append("Evidence:")
            for item in self.evidence:
                tool_id = item.get("tool", "")
                display = TOOL_DISPLAY.get(tool_id, tool_id)
                parsed_data = item.get("parsed")
                summary = _summarize_evidence(tool_id, parsed_data)
                lines.append(f"  - {display}: {summary}")

        if self.llm_diagnosis:
            lines.append("")
            if hasattr(self.llm_diagnosis, "pretty"):
                lines.append(self.llm_diagnosis.pretty())
            elif isinstance(self.llm_diagnosis, dict):
                lines.append("=" * 50)
                lines.append("LLM-ASSISTED DIAGNOSIS")
                lines.append("=" * 50)
                lines.append(f"Diagnosis: {self.llm_diagnosis.get('diagnosis', '')}")
                lines.append(f"Summary:   {self.llm_diagnosis.get('summary', '')}")
            else:
                lines.append(str(self.llm_diagnosis))

        return "\n".join(lines)