"""Rules mode (rules -> ML -> LLM) end to end for each scenario."""
import json

from analyzer.llm_client import LLMUnavailable
from analyzer.rules_engine import diagnose
from tests.scenarios import HOST, PEER

QUESTION = "My BGP peer is stuck at active state"
FULL_CHAIN = ["bgp_state", "interface", "tcp_port", "config"]


def test_healthy_stops_after_bgp_state(lab):
    v = diagnose(lab("healthy"), QUESTION, HOST, PEER)
    assert (v.resolved, v.source, v.checked) == (True, "rules", ["bgp_state"])
    assert v.root_cause == f"BGP peer {PEER} is Established"
    assert v.ml_prediction["label"] == "healthy"


def test_interface_down_found_by_rules(lab):
    v = diagnose(lab("interface_down"), QUESTION, HOST, PEER)
    assert (v.resolved, v.source, v.checked) == (True, "rules", ["bgp_state", "interface"])
    assert v.root_cause == "Interface(s) down: eth1"


def test_tcp_blocked_found_by_rules(lab):
    v = diagnose(lab("tcp_blocked"), QUESTION, HOST, PEER)
    assert (v.resolved, v.source, v.checked) == (True, "rules", ["bgp_state", "interface", "tcp_port"])
    assert "port 179" in v.root_cause


def test_config_drift_found_by_rules_with_baseline(lab):
    v = diagnose(lab("config_drift"), QUESTION, HOST, PEER)
    assert (v.resolved, v.source, v.checked) == (True, "rules", FULL_CHAIN)
    assert "drifted" in v.root_cause


def test_neighbor_shutdown_found_by_ml(lab):
    """GuideToRun Step 9: no rule fires, the ML engine names the fault."""
    v = diagnose(lab("neighbor_shutdown"), QUESTION, HOST, PEER)
    assert (v.resolved, v.source, v.checked) == (True, "ml", FULL_CHAIN)
    assert v.ml_prediction["label"] == "neighbor_shutdown"


def test_neighbor_shutdown_unresolved_without_ml(lab):
    v = diagnose(lab("neighbor_shutdown"), QUESTION, HOST, PEER, use_ml=False, use_llm=False)
    assert (v.resolved, v.source, v.ml_prediction) == (False, None, None)


def test_device_unreachable_found_by_ml(lab):
    v = diagnose(lab("device_unreachable"), QUESTION, HOST, PEER)
    assert v.checked == FULL_CHAIN
    assert all(e["success"] is False for e in v.evidence)
    assert (v.source, v.ml_prediction["label"]) == ("ml", "device_unreachable")


def test_remote_as_mismatch_goes_to_llm(lab, scripted_llm):
    """GuideToRun Step 10: rules find nothing, ML is unsure, the LLM decides."""
    llm = scripted_llm({
        "resolved": True, "root_cause": "remote-as mismatch", "confidence": "high",
        "suggested_fix": "router bgp 65001 / neighbor 172.20.20.3 remote-as 65002",
        "next_checks": ["show bgp neighbors 172.20.20.3"],
    })
    v = diagnose(lab("remote_as_mismatch"), "My BGP peer won't come up", HOST, PEER)

    assert (v.resolved, v.source, v.confidence) == (True, "llm", "high")
    assert v.root_cause == "remote-as mismatch"
    assert v.next_checks == ["show bgp neighbors 172.20.20.3"]
    assert v.notes == ["Diagnosed by scripted-llm"]
    assert v.ml_prediction["label"] != "remote_as_mismatch" or v.ml_prediction["confidence"] < 0.7

    (call,) = llm.calls
    assert "won't come up" in call["user"]
    case = json.loads(call["user"].split("\n\n", 1)[1])
    assert case["tools_checked_in_order"] == FULL_CHAIN
    assert "remote-as 65009" in json.dumps(case["evidence"])


def test_llm_unavailable_leaves_case_unresolved(lab, scripted_llm):
    scripted_llm(LLMUnavailable("could not reach Ollama"))
    v = diagnose(lab("remote_as_mismatch"), QUESTION, HOST, PEER, use_ml=False)
    assert (v.resolved, v.source) == (False, None)
    assert v.notes == ["LLM escalation skipped: could not reach Ollama"]


def test_no_llm_flag_skips_llm(lab):
    v = diagnose(lab("remote_as_mismatch"), QUESTION, HOST, PEER, use_ml=False, use_llm=False)
    assert v.resolved is False
    assert v.notes == []


def test_triage_keywords_pick_starting_intent(lab):
    v = diagnose(lab("interface_down"), "Is the interface to my peer up?", HOST, PEER)
    assert v.checked == ["interface"]
