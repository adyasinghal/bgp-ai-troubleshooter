"""rules_engine.evaluate: findings and hints."""
import pytest

from analyzer.rules_engine import evaluate
from analyzer.tool_registry import ToolRegistry
from tests.scenarios import HOST, PEER

CTX = {"host": HOST, "peer": PEER}


def finding(lab, scenario: str, tool_id: str) -> dict:
    client = lab(scenario)
    result = ToolRegistry.load(client).execute(client, tool_id, {}, CTX)
    return evaluate(tool_id, result, CTX)


@pytest.mark.parametrize("scenario, starts_with", [
    ("neighbor_shutdown", "Idle (Admin): the neighbor is administratively shut down"),
    ("remote_as_mismatch", "Idle with no reason: the neighbor is NOT administratively shut down"),
    ("tcp_blocked", "Connect: the TCP session to the peer is not forming"),
    ("interface_down", "Active: the TCP session to the peer is not forming"),
])
def test_bgp_state_hints(lab, scenario, starts_with):
    f = finding(lab, scenario, "bgp_state")
    assert f["decision"] == "continue"
    assert f["hint"].startswith(starts_with)


def test_unlisted_peer_hint():
    result = {"success": True, "parsed": {"queried_peer_state": "unknown", "queried_peer_state_reason": None}}
    assert "not configured" in evaluate("bgp_state", result, CTX)["hint"]


def test_config_facts_shut_down(lab):
    hint = finding(lab, "neighbor_shutdown", "config")["hint"]
    assert "local AS 65001" in hint and "remote-as 65002" in hint and "IS shut down" in hint


def test_config_facts_remote_as(lab):
    hint = finding(lab, "remote_as_mismatch", "config")["hint"]
    assert "remote-as 65009" in hint and "NOT shut down" in hint and "remote-as mismatch" in hint


def test_config_facts_missing_neighbor():
    result = {"success": True, "parsed": {"has_baseline": False},
              "raw_output": "router bgp 65001\n neighbor 10.0.0.9 remote-as 65003\nexit"}
    assert "neighbor_missing" in evaluate("config", result, CTX)["hint"]


def test_config_with_baseline_decides(lab):
    f = finding(lab, "config_drift", "config")
    assert (f["decision"], f["fault_class"]) == ("root_cause", "config_drift")


def test_ssh_failure_hint(lab):
    f = finding(lab, "device_unreachable", "bgp_state")
    assert f["decision"] == "continue" and "device_unreachable" in f["hint"]


def test_other_failure_has_no_hint():
    assert evaluate("bgp_state", {"success": False, "error": "HTTPError: 500"}, CTX) == {"decision": "continue"}


@pytest.mark.parametrize("scenario, decision, fault_class, fix", [
    ("healthy", "healthy", "healthy", "No action needed — the session is up."),
    ("neighbor_shutdown", "root_cause", "neighbor_shutdown", "router bgp 65001 / no neighbor 172.20.20.3 shutdown"),
    ("remote_as_mismatch", "root_cause", "remote_as_mismatch", "router bgp 65001 / neighbor 172.20.20.3 remote-as 65002"),
    ("peer_deconfigured", "root_cause", "neighbor_missing",
     "on router2: router bgp 65002 / neighbor 172.20.20.2 remote-as 65001"),
])
def test_neighbor_findings(lab, scenario, decision, fault_class, fix):
    f = finding(lab, scenario, "bgp_neighbor")
    assert (f["decision"], f["fault_class"], f["fix"]) == (decision, fault_class, fix)


def test_neighbor_remote_as_cause_names_both_asns(lab):
    assert finding(lab, "remote_as_mismatch", "bgp_neighbor")["cause"] == (
        "This router expects AS 65009 for 172.20.20.3, but the peer uses AS 65002")


def test_neighbor_never_up_is_a_hint(lab):
    f = finding(lab, "tcp_blocked", "bgp_neighbor")
    assert f["decision"] == "continue"
    assert "never come up" in f["hint"] and "TCP port 179" in f["hint"]


def test_neighbor_rejected_by_peer():
    parsed = {"configured": True, "state": "Idle", "local_as": 65001, "remote_as": 65002,
              "local_host": "172.20.20.2", "hostname": "router2",
              "notification": {"direction": "received", "error": "OPEN Message Error/Bad Peer AS"}}
    f = evaluate("bgp_neighbor", {"success": True, "parsed": parsed}, CTX)
    assert f["fault_class"] == "remote_as_mismatch"
    assert f["fix"] == "on router2: router bgp 65002 / neighbor 172.20.20.2 remote-as 65001"


def test_neighbor_not_configured():
    f = evaluate("bgp_neighbor", {"success": True, "parsed": {"configured": False}}, CTX)
    assert (f["decision"], f["fault_class"]) == ("root_cause", "neighbor_missing")
