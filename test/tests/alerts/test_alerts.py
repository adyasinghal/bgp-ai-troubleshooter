from pathlib import Path

import pytest

from alerts.bridge import verdict_to_alert
from alerts.evaluator import AlertEvaluator
from alerts.monitor import NetworkMonitor
from alerts.models import AlertSeverity, AlertType
from alerts.presentation import format_indian_time, split_current_and_resolved
from alerts.state_tracker import AlertStateTracker
from alerts.integration import publish_verdict
from alerts.store import (
    AlertStoreError,
    append_alert,
    load_alerts,
    record_transition,
    restore_tracker,
)
from tools.base_tool import ToolResult
from tools.device_client import DeviceResult
from tools.interface import InterfaceTool
from tools.tcp_port import TCPPortTool


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


@pytest.mark.parametrize(("output", "expected"), [("REACHABLE", True), ("UNREACHABLE", False)])
def test_tcp_tool_parses_reachability_output_exactly(output: str, expected: bool) -> None:
    class FakeDeviceClient:
        @staticmethod
        def run_raw(host: str, command: str) -> DeviceResult:
            return DeviceResult(host, command, True, output)

    result = TCPPortTool(FakeDeviceClient()).run("router1", "10.0.0.2")

    assert result.parsed["reachable"] is expected


@pytest.mark.parametrize(
    ("peer_ip", "port"),
    [("10.0.0.2; echo injected", 179), ("10.0.0.2", 0), ("10.0.0.2", 65536)],
)
def test_tcp_tool_rejects_invalid_peer_or_port(peer_ip: str, port: int) -> None:
    class UnusedDeviceClient:
        @staticmethod
        def run_raw(host: str, command: str) -> DeviceResult:
            raise AssertionError("invalid TCP probe inputs must not reach the device")

    with pytest.raises(ValueError):
        TCPPortTool(UnusedDeviceClient()).run("router1", peer_ip, port)


def test_interface_tool_uses_fr_router_interface_command() -> None:
    class FakeDeviceClient:
        command = ""

        def run_vtysh(self, host: str, command: str) -> DeviceResult:
            self.command = command
            return DeviceResult(
                host,
                command,
                True,
                "Interface eth1 is up, line protocol is up\n  inet 10.0.0.1/24",
            )

    client = FakeDeviceClient()
    result = InterfaceTool(client).run("router1")

    assert client.command == "show interface"
    assert result.parsed["interfaces"]["eth1"]["link_state"] == "up"


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


def test_transitions_deduplicate_across_trackers_and_persist_recovery(tmp_path: Path) -> None:
    log_path = tmp_path / "alerts.json"
    first_tracker = AlertStateTracker()
    second_tracker = AlertStateTracker()
    fault = AlertEvaluator().evaluate(
        [tool_result("bgp_state", {"queried_peer_state": "Idle"})],
        "router1",
        "10.0.0.2",
    )
    assert fault is not None

    assert record_transition(first_tracker, "router1", "10.0.0.2", fault, log_path)
    repeated = AlertEvaluator().evaluate(
        [tool_result("bgp_state", {"queried_peer_state": "Idle"})],
        "router1",
        "10.0.0.2",
    )
    assert repeated is not None
    assert record_transition(second_tracker, "router1", "10.0.0.2", repeated, log_path) is None

    recovery = record_transition(second_tracker, "router1", "10.0.0.2", None, log_path)

    assert recovery is not None
    assert recovery.severity is AlertSeverity.RECOVERY
    assert "BGP_STATE: Idle" in recovery.cause
    assert [event["severity"] for event in load_alerts(log_path)] == [
        "WARNING",
        "RECOVERY",
    ]


def test_transition_deduplicates_analyzer_alert_without_bgp_state(
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "alerts.json"
    tracker = AlertStateTracker()
    monitor_alert = AlertEvaluator().evaluate(
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
    assert monitor_alert is not None
    assert record_transition(tracker, "router1", "10.0.0.2", monitor_alert, log_path)

    analyzer_alert = verdict_to_alert(
        {
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
        },
        "router1",
        "10.0.0.2",
    )

    assert analyzer_alert is not None
    assert analyzer_alert.current_state is None
    assert record_transition(
        AlertStateTracker(), "router1", "10.0.0.2", analyzer_alert, log_path
    ) is None
    assert len(load_alerts(log_path)) == 1


def test_analyzer_verdicts_share_persisted_fault_and_recovery_lifecycle(
    tmp_path: Path,
) -> None:
    device = "integration-test-router"
    peer = "192.0.2.2"
    fault = {
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
    healthy = {
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

    assert publish_verdict(fault, device, peer, tmp_path / "alerts.json") is not None
    assert publish_verdict(fault, device, peer, tmp_path / "alerts.json") is None
    recovery = publish_verdict(healthy, device, peer, tmp_path / "alerts.json")

    assert recovery is not None
    assert recovery.severity is AlertSeverity.RECOVERY
    assert [
        event["severity"]
        for event in load_alerts(tmp_path / "alerts.json")
    ] == ["CRITICAL", "RECOVERY"]


def test_dashboard_separates_current_incidents_from_resolved_history() -> None:
    active, resolved = split_current_and_resolved(
        [
            {"device": "r1", "neighbor": "p1", "severity": "CRITICAL", "timestamp": "1"},
            {"device": "r1", "neighbor": "p1", "severity": "RECOVERY", "timestamp": "2"},
            {"device": "r2", "neighbor": "p2", "severity": "WARNING", "timestamp": "3"},
        ]
    )

    assert [(event["device"], event["severity"]) for event in active] == [
        ("r2", "WARNING")
    ]
    assert [(event["device"], event["severity"]) for event in resolved] == [
        ("r1", "RECOVERY")
    ]


def test_dashboard_formats_utc_time_in_indian_standard_time() -> None:
    assert format_indian_time("2026-09-27T00:00:00+00:00") == (
        "27 Sep 2026, 05:30:00 AM IST"
    )


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
