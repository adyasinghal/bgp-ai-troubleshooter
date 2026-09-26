"""Run a router-free, end-to-end demo of the alert monitor and recovery path."""

from __future__ import annotations

import tempfile
from pathlib import Path

from alerts.evaluator import AlertEvaluator
from alerts.models import AlertSeverity
from alerts.monitor import NetworkMonitor
from alerts.state_tracker import AlertStateTracker
from alerts.store import load_alerts
from tools.base_tool import ToolResult


def result(
    tool_id: str,
    parsed: dict | None = None,
    *,
    success: bool = True,
    error: str | None = None,
) -> ToolResult:
    return ToolResult(
        tool_id=tool_id,
        host="router1",
        command=f"show {tool_id}",
        success=success,
        raw_output="",
        parsed=parsed or {},
        error=error,
    )


def console_safe(text: str) -> str:
    return (
        text.replace("\u2192", "->")
        .replace("\u2014", "-")
        .replace("\u2013", "-")
    )


class ScenarioTool:
    def __init__(self, tool_id: str, responses: list[ToolResult]) -> None:
        self.tool_id = tool_id
        self.responses = iter(responses)

    def run(self, host: str, *args) -> ToolResult:
        response = next(self.responses)
        response.host = host
        return response


def main() -> None:
    peer = "10.0.0.2"
    healthy_bgp = result("bgp_state", {"queried_peer_state": "Established"})
    healthy_interface = result(
        "interface",
        {"interfaces": {"eth1": {"link_state": "up", "admin_state": "up"}}},
    )
    healthy_tcp = result("tcp_port", {"reachable": True, "port": 179})
    no_drift = result("config", {"has_baseline": True, "drifted": False})

    with tempfile.TemporaryDirectory(prefix="alerts-demo-") as temp_dir:
        log_path = Path(temp_dir) / "alerts.json"
        monitor = NetworkMonitor.__new__(NetworkMonitor)
        monitor.interval = 1
        monitor.log_path = log_path
        monitor.evaluator = AlertEvaluator()
        monitor.tracker = AlertStateTracker()
        monitor.bgp_tool = ScenarioTool(
            "bgp_state",
            [
                result("bgp_state", {"queried_peer_state": "Active"}),
                result("bgp_state", {"queried_peer_state": "Active"}),
                healthy_bgp,
                result("bgp_state", {}, success=False, error="SSH timeout"),
                healthy_bgp,
            ],
        )
        monitor.interface_tool = ScenarioTool(
            "interface",
            [
                result(
                    "interface",
                    {"interfaces": {"eth1": {"link_state": "down", "admin_state": "up"}}},
                ),
                result(
                    "interface",
                    {"interfaces": {"eth1": {"link_state": "down", "admin_state": "up"}}},
                ),
                healthy_interface,
                result("interface", {}, success=False, error="SSH timeout"),
                healthy_interface,
            ],
        )
        monitor.tcp_tool = ScenarioTool(
            "tcp_port",
            [
                result("tcp_port", {"reachable": False, "port": 179}),
                result("tcp_port", {"reachable": False, "port": 179}),
                healthy_tcp,
                result("tcp_port", {}, success=False, error="SSH timeout"),
                healthy_tcp,
            ],
        )
        monitor.config_tool = ScenarioTool(
            "config",
            [
                no_drift,
                no_drift,
                no_drift,
                result("config", {}, success=False, error="SSH timeout"),
                no_drift,
            ],
        )

        scenario_names = (
            "1. BGP Active, interface down, and TCP/179 unreachable",
            "2. Same fault on the next poll (deduplication)",
            "3. BGP and interface return healthy (recovery)",
            "4. Router becomes unreachable over SSH",
            "5. Router responds and BGP returns Established (recovery)",
        )
        for name in scenario_names:
            before = len(load_alerts(log_path))
            monitor.poll_device("router1", peer)
            events = load_alerts(log_path)
            emitted = events[-1] if len(events) > before else None
            if emitted:
                state = emitted.get("current_state") or "n/a"
                print(
                    f"{name}\n"
                    f"   emitted: {emitted['severity']} {emitted['alert_type']} "
                    f"state={state} - {console_safe(emitted['message'])}"
                )
            else:
                print(f"{name}\n   emitted: none (unchanged incident suppressed)")

        history = load_alerts(log_path)
        expected = ["CRITICAL", "RECOVERY", "CRITICAL", "RECOVERY"]
        actual = [event["severity"] for event in history]
        if actual != expected:
            raise AssertionError(f"Unexpected event sequence: {actual!r}")
        if history[2]["current_state"] != "Unavailable":
            raise AssertionError("SSH outage was not reported as unavailable")
        if history[2]["severity"] != AlertSeverity.CRITICAL.value:
            raise AssertionError("SSH outage did not create a critical alert")

        print(f"\nPersisted history: {len(history)} events (expected {len(expected)})")
        print("End-to-end monitor demo: PASS")


if __name__ == "__main__":
    main()
