from pathlib import Path

import pytest

from alerts.bridge import verdict_to_alert
from alerts.evaluator import AlertEvaluator
from alerts.monitor import NetworkMonitor
from alerts.models import AlertSeverity, AlertType
from alerts.state_tracker import AlertStateTracker
from alerts.store import AlertStoreError, append_alert, load_alerts, restore_tracker
from tools.base_tool import ToolResult


def tool_result(tool_id: str, parsed: dict, success: bool = True, error: str = "") -> ToolResult:
    return ToolResult(
        tool_id=tool_id,
        host="router1",
        command=f"show {tool_id}",
        success=success,
        raw_output="",
        parsed=parsed,
        error=error or None,
    )


def test_tool_result_contract_detects_unreachable_tcp_peer() -> None:
    payload = tool_result(
        "tcp_port", {"reachable": False, "port": 179}
    ).to_dict()
    result = ToolResult(**payload)
    tools = [
        tool_result("bgp_state", {"queried_peer_state": "Active"}),
        result,
    ]

    alert = AlertEvaluator().evaluate(tools, "router1", "10.0.0.2")

    assert alert is not None
    assert alert.alert_type is AlertType.TCP
    assert alert.severity is AlertSeverity.CRITICAL
    assert alert.evidence["tcp_reachable"] is False


def test_interface_down_creates_critical_alert() -> None:
    alert = AlertEvaluator().evaluate(
        [
            tool_result("bgp_state", {"queried_peer_state": "Active"}),
            tool_result(
                "interface",
                {"interfaces": {"eth1": {"link_state": "down", "admin_state": "up"}}},
            ),
        ],
        "router1",
        "10.0.0.2",
    )

    assert alert is not None
    assert alert.alert_type is AlertType.INTERFACE
    assert alert.severity is AlertSeverity.CRITICAL


def test_tool_failure_is_reported_without_trusting_parsed_state() -> None:
    alert = AlertEvaluator().evaluate(
        [
            tool_result(
                "bgp_state",
                {"queried_peer_state": "Established"},
                success=False,
                error="SSH timeout",
            )
        ],
        "router1",
        "10.0.0.2",
    )

    assert alert is not None
    assert alert.severity is AlertSeverity.CRITICAL
    assert alert.current_state == "Unavailable"
    assert "bgp_state" in alert.evidence["tool_failures"]
    assert "bgp_state" not in alert.evidence


def test_configuration_drift_requires_a_known_baseline() -> None:
    evaluator = AlertEvaluator()
    tools = [
        tool_result("bgp_state", {"queried_peer_state": "Established"}),
        tool_result("config", {"has_baseline": False, "drifted": True}),
    ]

    assert evaluator.evaluate(tools, "router1", "10.0.0.2") is None

    tools[1] = tool_result(
        "config", {"has_baseline": True, "drifted": True, "diff": ["router bgp"]}
    )
    alert = evaluator.evaluate(tools, "router1", "10.0.0.2")
    assert alert is not None
    assert alert.alert_type is AlertType.CONFIG
    assert alert.severity is AlertSeverity.WARNING


def test_analyzer_bridge_keeps_resolved_fault_and_suppresses_established_peer() -> None:
    fault_verdict = {
        "resolved": True,
        "source": "rules",
        "root_cause": "Interface(s) down: eth1",
        "suggested_fix": "Bring the interface up",
        "evidence": [
            {
                "tool": "interface",
                "success": True,
                "parsed": {
                    "interfaces": {
                        "eth1": {"link_state": "down", "admin_state": "up"}
                    }
                },
            }
        ],
    }
    healthy_verdict = {
        "resolved": True,
        "source": "rules",
        "evidence": [
            {
                "tool": "bgp_state",
                "success": True,
                "parsed": {"queried_peer_state": "Established"},
            }
        ],
    }

    alert = verdict_to_alert(fault_verdict, "router1", "10.0.0.2")

    assert alert is not None
    assert alert.alert_type is AlertType.INTERFACE
    assert verdict_to_alert(healthy_verdict, "router1", "10.0.0.2") is None


def test_store_persists_history_and_restores_recovery_state(tmp_path: Path) -> None:
    log_path = tmp_path / "alerts.json"
    tracker = AlertStateTracker()
    active_alert = AlertEvaluator().evaluate(
        [tool_result("bgp_state", {"queried_peer_state": "Idle"})],
        "router1",
        "10.0.0.2",
    )
    assert active_alert is not None
    active = tracker.check_and_update(active_alert)
    assert active is not None
    append_alert(active, log_path)

    recovery = tracker.record_healthy("router1", "10.0.0.2")
    assert recovery is not None
    append_alert(recovery, log_path)
    assert len(load_alerts(log_path)) == 2

    restored = AlertStateTracker()
    restore_tracker(restored, log_path)
    assert restored.current_state("router1", "10.0.0.2").severity is AlertSeverity.RECOVERY
    assert restored.record_healthy("router1", "10.0.0.2") is None


def test_corrupt_store_is_reported_without_reset(tmp_path: Path) -> None:
    log_path = tmp_path / "alerts.json"
    log_path.write_text("{invalid", encoding="utf-8")

    with pytest.raises(AlertStoreError):
        load_alerts(log_path)

    assert log_path.read_text(encoding="utf-8") == "{invalid"


def test_monitor_converts_tool_exception_to_failed_tool_result() -> None:
    class BrokenTool:
        tool_id = "bgp_state"

        @staticmethod
        def run(host: str, *args) -> ToolResult:
            raise TimeoutError("router unavailable")

    result = NetworkMonitor._run_tool(BrokenTool(), "router1")

    assert not result.success
    assert result.host == "router1"
    assert result.tool_id == "bgp_state"
    assert "TimeoutError" in result.error
