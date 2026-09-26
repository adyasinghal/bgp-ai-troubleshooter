"""Apply evidence-based rules to tool results and produce an Alert."""

from __future__ import annotations

import logging
from typing import Optional

from tools.base_tool import ToolResult
from alerts.models import Alert, AlertSeverity, AlertType

logger = logging.getLogger(__name__)

_UNHEALTHY_BGP_STATES = {"Active", "Idle", "Connect", "OpenSent", "OpenConfirm"}
_HEALTHY_BGP_STATE = "Established"


class AlertEvaluator:
    """Convert parsed tool results into an alert or a healthy result."""

    def evaluate(
        self,
        tool_results: list[ToolResult],
        device: str,
        neighbor: str,
        diagnosis: str = "",
    ) -> Optional[Alert]:
        """
        Evaluate a collection of ToolResult objects and return an Alert if
        the evidence warrants one, or None if everything looks healthy.

        Parameters
        ----------
        tool_results : list[ToolResult]
            Results from one or more tools in the cohort.  The evaluator
            inspects each one by tool_id.
        device : str
            The FRR node / router being monitored (e.g. "leaf1").
        neighbor : str
            The BGP peer IP being investigated (e.g. "10.0.0.2").
        diagnosis : str
            Optional free-text diagnosis from the LLM / reasoning loop.
            Included in the alert cause when provided.
        """
        parsed: dict[str, dict] = {}
        failed_tools = [result for result in tool_results if not result.success]
        for tr in tool_results:
            if tr.success:
                parsed[tr.tool_id] = tr.parsed or {}
            if not tr.success:
                logger.warning(
                    "ToolResult for '%s' on host '%s' reported failure: %s",
                    tr.tool_id,
                    tr.host,
                    tr.error,
                )

        bgp_parsed = parsed.get("bgp_state", {})
        iface_parsed = parsed.get("interface", {})
        tcp_parsed = parsed.get("tcp_port", {})
        cfg_parsed = parsed.get("config", {})

        bgp_state = self._get_bgp_state(bgp_parsed, neighbor)

        evidence = self._build_evidence(bgp_state, bgp_parsed, iface_parsed, tcp_parsed, cfg_parsed, neighbor)

        if failed_tools:
            failures = {
                result.tool_id: {
                    "command": result.command,
                    "error": result.error or "Tool execution failed.",
                }
                for result in failed_tools
            }
            evidence["tool_failures"] = failures
            failed_names = ", ".join(failures)
            severity = (
                AlertSeverity.CRITICAL
                if any(result.tool_id in {"bgp_state", "tcp_port"} for result in failed_tools)
                else AlertSeverity.WARNING
            )
            return Alert(
                device=device,
                neighbor=neighbor,
                alert_type=AlertType.BGP_STATE,
                severity=severity,
                current_state=bgp_state or "Unavailable",
                message=f"Telemetry collection failed for {device}: {failed_names}.",
                cause="One or more device checks failed; router state could not be confirmed.",
                recommended_action="Check router reachability and SSH access, then rerun the telemetry checks.",
                evidence=evidence,
            )

        alert = (
            self._check_bgp_active_with_interface_down(bgp_state, iface_parsed, evidence, device, neighbor, diagnosis)
            or self._check_bgp_active_with_tcp_unreachable(bgp_state, tcp_parsed, evidence, device, neighbor, diagnosis)
            or self._check_bgp_idle(bgp_state, evidence, device, neighbor, diagnosis)
            or self._check_bgp_active_generic(bgp_state, iface_parsed, tcp_parsed, evidence, device, neighbor, diagnosis)
            or self._check_config_drift(bgp_state, cfg_parsed, evidence, device, neighbor, diagnosis)
        )

        if alert is None:
            logger.debug(
                "No alert generated for device=%r neighbor=%r bgp_state=%r",
                device,
                neighbor,
                bgp_state,
            )
        else:
            logger.info(
                "Alert generated: severity=%s type=%s device=%r neighbor=%r",
                alert.severity.value,
                alert.alert_type.value,
                device,
                neighbor,
            )

        return alert

    def _get_bgp_state(self, bgp_parsed: dict, neighbor: str) -> Optional[str]:
        """
        Pull the BGP state for the queried neighbor out of bgp_state tool output.

        BGPStateTool returns:
            { "peers": {"10.0.0.2": "Active"}, "queried_peer_state": "Active" }
        """
        state = bgp_parsed.get("queried_peer_state")
        if state and state != "unknown":
            return state
        peers: dict = bgp_parsed.get("peers", {})
        if neighbor in peers:
            return peers[neighbor]
        if len(peers) == 1:
            return next(iter(peers.values()))
        return None

    def _build_evidence(
        self,
        bgp_state: Optional[str],
        bgp_parsed: dict,
        iface_parsed: dict,
        tcp_parsed: dict,
        cfg_parsed: dict,
        neighbor: str,
    ) -> dict:
        evidence: dict = {}
        if bgp_state is not None:
            evidence["bgp_state"] = bgp_state

        interfaces: dict = iface_parsed.get("interfaces", {})
        if interfaces:
            evidence["interfaces"] = {
                name: {
                    "link_state": info.get("link_state"),
                    "admin_state": info.get("admin_state"),
                    "ip_address": info.get("ip_address"),
                }
                for name, info in interfaces.items()
            }

        if tcp_parsed:
            evidence["tcp_reachable"] = tcp_parsed.get("reachable")
            evidence["tcp_port"] = tcp_parsed.get("port", 179)

        if cfg_parsed.get("has_baseline") and cfg_parsed.get("drifted") is not None:
            evidence["config_drifted"] = cfg_parsed.get("drifted")

        return evidence

    def _check_bgp_active_with_interface_down(
        self,
        bgp_state: Optional[str],
        iface_parsed: dict,
        evidence: dict,
        device: str,
        neighbor: str,
        diagnosis: str,
    ) -> Optional[Alert]:
        """CRITICAL: BGP not Established AND at least one interface is down."""
        if bgp_state not in _UNHEALTHY_BGP_STATES:
            return None

        interfaces: dict = iface_parsed.get("interfaces", {})
        down_ifaces = [
            name
            for name, info in interfaces.items()
            if info.get("link_state", "up").lower() == "down"
               or info.get("admin_state", "up").lower() == "down"
        ]
        if not down_ifaces:
            return None

        iface_list = ", ".join(down_ifaces)
        cause = (
            f"Interface(s) [{iface_list}] connected toward the BGP peer are DOWN. "
            + (diagnosis if diagnosis else "")
        ).strip()

        return Alert(
            device=device,
            neighbor=neighbor,
            alert_type=AlertType.INTERFACE,
            severity=AlertSeverity.CRITICAL,
            current_state=bgp_state,
            message=(
                f"BGP session to {neighbor} is stuck in {bgp_state.upper()} state "
                f"because the associated interface is DOWN."
            ),
            cause=cause,
            recommended_action=(
                f"Bring interface(s) [{iface_list}] UP and verify that the BGP session "
                f"transitions to ESTABLISHED. "
                f"Check with: 'show interface {down_ifaces[0]}' and 'show bgp summary'."
            ),
            evidence=evidence,
        )

    def _check_bgp_active_with_tcp_unreachable(
        self,
        bgp_state: Optional[str],
        tcp_parsed: dict,
        evidence: dict,
        device: str,
        neighbor: str,
        diagnosis: str,
    ) -> Optional[Alert]:
        """CRITICAL: BGP not Established AND TCP port 179 is unreachable."""
        if bgp_state not in _UNHEALTHY_BGP_STATES:
            return None
        if not tcp_parsed:
            return None
        if tcp_parsed.get("reachable", True):
            return None

        port = tcp_parsed.get("port", 179)
        cause = (
            f"TCP connectivity to BGP peer {neighbor} on port {port} is unavailable. "
            + (diagnosis if diagnosis else "")
        ).strip()

        return Alert(
            device=device,
            neighbor=neighbor,
            alert_type=AlertType.TCP,
            severity=AlertSeverity.CRITICAL,
            current_state=bgp_state,
            message=(
                f"BGP session to {neighbor} is stuck in {bgp_state.upper()} state. "
                f"TCP/BGP port {port} is UNREACHABLE."
            ),
            cause=cause,
            recommended_action=(
                f"Check routing/ACL to {neighbor}, verify firewall rules permit TCP/179, "
                f"and confirm the peer's BGP daemon is running."
            ),
            evidence=evidence,
        )

    def _check_bgp_active_generic(
        self,
        bgp_state: Optional[str],
        iface_parsed: dict,
        tcp_parsed: dict,
        evidence: dict,
        device: str,
        neighbor: str,
        diagnosis: str,
    ) -> Optional[Alert]:
        """
        WARNING: BGP not Established but interface and TCP look OK —
        likely a config mismatch.
        """
        if bgp_state not in _UNHEALTHY_BGP_STATES:
            return None

        cause = (
            f"BGP session is in {bgp_state} state. "
            f"Interface and TCP layer appear reachable — possible configuration mismatch. "
            + (diagnosis if diagnosis else "")
        ).strip()

        return Alert(
            device=device,
            neighbor=neighbor,
            alert_type=AlertType.BGP_STATE,
            severity=AlertSeverity.WARNING,
            current_state=bgp_state,
            message=(
                f"BGP session to {neighbor} is in {bgp_state.upper()} state "
                f"and has not reached ESTABLISHED."
            ),
            cause=cause,
            recommended_action=(
                "Compare running-config against the peer's config for AS number, "
                "neighbor address, and authentication mismatches. "
                "Run 'show bgp neighbors <peer>' for detailed error output."
            ),
            evidence=evidence,
        )

    def _check_bgp_idle(
        self,
        bgp_state: Optional[str],
        evidence: dict,
        device: str,
        neighbor: str,
        diagnosis: str,
    ) -> Optional[Alert]:
        """WARNING: BGP Idle — session not even attempting to connect."""
        if bgp_state != "Idle":
            return None

        cause = (
            "BGP session is IDLE — the router is not attempting to establish the session. "
            + (diagnosis if diagnosis else "")
        ).strip()

        return Alert(
            device=device,
            neighbor=neighbor,
            alert_type=AlertType.BGP_STATE,
            severity=AlertSeverity.WARNING,
            current_state=bgp_state,
            message=f"BGP session to {neighbor} is IDLE and requires investigation.",
            cause=cause,
            recommended_action=(
                "Check 'show bgp neighbors' for the hold-down timer. "
                "Verify neighbor statement in router bgp config and that the peer IP is correct."
            ),
            evidence=evidence,
        )

    def _check_config_drift(
        self,
        bgp_state: Optional[str],
        cfg_parsed: dict,
        evidence: dict,
        device: str,
        neighbor: str,
        diagnosis: str,
    ) -> Optional[Alert]:
        """WARNING: Running-config has drifted from baseline (even if BGP is up)."""
        if not cfg_parsed.get("has_baseline") or not cfg_parsed.get("drifted"):
            return None

        diff_lines = cfg_parsed.get("diff", [])
        cause = (
            f"Running-config on {device} has {len(diff_lines)} line(s) of drift from the "
            f"saved baseline. This may be causing or contributing to BGP instability. "
            + (diagnosis if diagnosis else "")
        ).strip()

        return Alert(
            device=device,
            neighbor=neighbor,
            alert_type=AlertType.CONFIG,
            severity=AlertSeverity.WARNING,
            current_state=bgp_state,
            message=f"Configuration drift detected on {device}.",
            cause=cause,
            recommended_action=(
                "Review the diff output in the evidence field, identify unintended changes, "
                "and restore the known-good configuration or update the baseline."
            ),
            evidence=evidence,
        )
