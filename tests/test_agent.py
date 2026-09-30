"""The LLM agent loop, with the lab faked and the LLM's decisions scripted.

These test the loop's mechanics (guardrails, rule findings, budget, fallback,
verdict), not the quality of a real model's choices."""
import json

import pytest

from analyzer import agent, prompts
from analyzer.agent import investigate
from analyzer.llm_client import LLMUnavailable
from analyzer.ml_engine import FAULTS
from analyzer.run import main
from tests.fakes import call, conclude
from tests.scenarios import HOST, PEER

QUESTION = "My BGP peer is stuck at active state"


def case_of(llm_call: dict) -> dict:
    """The case JSON the agent sent in one LLM call."""
    return json.loads(llm_call["user"].split("\n\n", 1)[1].rsplit("\n\n", 1)[0])


def test_fault_classes_match_ml_labels():
    assert set(prompts.FAULT_CLASSES) - {"other"} <= set(FAULTS)
    assert prompts.diagnosis_schema()["properties"]["fault_class"]["enum"] == list(prompts.FAULT_CLASSES)


def test_neighbor_shutdown_in_two_tool_calls(lab, scripted_llm):
    """The walk-through from the plan: bgp_state shows Idle (Admin), config confirms."""
    llm = scripted_llm(
        call("bgp_state", "bgp_state_check"),
        call("config", "config_check", thought="Idle (Admin) means shut down; confirm in config"),
        conclude("neighbor_shutdown", "Neighbor 172.20.20.3 is shut down", fix="no neighbor 172.20.20.3 shutdown"),
    )
    v = investigate(lab("neighbor_shutdown"), QUESTION, HOST, PEER)

    assert (v.resolved, v.source, v.fault_class, v.confidence) == (True, "agent", "neighbor_shutdown", "high")
    assert v.checked == ["bgp_state", "config"]
    assert v.rules_agree is None                    # no rule reached a decision
    assert v.ml_prediction["label"] == "neighbor_shutdown"
    assert v.notes == ["Diagnosed by scripted-llm"]
    assert [s["summary"] for s in v.steps] == ["peer Idle (Admin)", "running config read (no baseline)"]
    assert v.steps[1]["thought"] == "Idle (Admin) means shut down; confirm in config"
    assert v.triage == {"method": "keyword", "suggested_first_intent": "bgp_state_check"}

    first, second, third = (case_of(c) for c in llm.calls)
    assert first["steps"] == [] and first["question"] == QUESTION
    assert {t["tool_id"] for t in first["tools"]} >= {"bgp_state", "config", "ml_classify"}
    step1 = second["steps"][0]
    assert step1["result"]["parsed"]["queried_peer_state_reason"] == "Admin"
    assert step1["rule_finding"]["decision"] == "continue"
    assert step1["rule_finding"]["suggested_next_intent"] == "interface_check"
    assert step1["rule_finding"]["hint"].startswith("Idle (Admin): the neighbor is administratively shut down")
    # config without a baseline: the redundant diff is dropped, the running config stays
    step2 = third["steps"][1]["result"]
    assert "no baseline" in step2["parsed"]["diff"]
    assert "neighbor 172.20.20.3 shutdown" in step2["raw_output"]


def test_rule_finding_shown_and_agreement(lab, scripted_llm):
    llm = scripted_llm(
        call("bgp_state"), call("interface"),
        conclude("interface_down", "eth1 is down"),
    )
    v = investigate(lab("interface_down"), QUESTION, HOST, PEER)
    finding = case_of(llm.calls[2])["steps"][1]["rule_finding"]
    assert (finding["decision"], finding["fault_class"]) == ("root_cause", "interface_down")
    # a decided finding tells the LLM to conclude instead of pointing down the chain
    assert "suggested_next_intent" not in finding and "Conclude now" in finding["note"]
    assert v.rules_agree is True
    assert v.steps[1]["summary"] == "down: eth1 [rule: root_cause]"
    assert "Rules agree:   yes" in v.pretty()


def test_rules_disagreement_is_flagged(lab, scripted_llm):
    scripted_llm(call("bgp_state"), call("interface"), conclude("remote_as_mismatch"))
    v = investigate(lab("interface_down"), QUESTION, HOST, PEER)
    assert v.rules_agree is False
    assert "Rules agree:   NO" in v.pretty()


def test_specific_config_fault_agrees_with_config_drift(lab, scripted_llm):
    scripted_llm(call("bgp_state"), call("config"), conclude("remote_as_mismatch"))
    v = investigate(lab("config_drift"), QUESTION, HOST, PEER)
    assert v.rules_agree is True


def test_healthy(lab, scripted_llm):
    scripted_llm(call("bgp_state"), conclude("healthy", "Session is Established"))
    v = investigate(lab("healthy"), QUESTION, HOST, PEER)
    assert (v.resolved, v.fault_class, v.rules_agree) == (True, "healthy", True)
    assert v.ml_prediction["label"] == "healthy"


def test_trust_rules_stops_at_first_finding(lab, scripted_llm):
    llm = scripted_llm(call("bgp_state"), call("interface"))
    v = investigate(lab("interface_down"), QUESTION, HOST, PEER, trust_rules=True)
    assert (v.source, v.root_cause, v.fault_class) == ("rules", "Interface(s) down: eth1", "interface_down")
    assert len(llm.calls) == 2
    assert v.steps[-1]["tool"] == "interface"


def test_interface_arg_reaches_the_device(lab, scripted_llm):
    client = lab("interface_down")
    scripted_llm(call("bgp_state"), call("interface", interface="eth1"), conclude("interface_down"))
    v = investigate(client, QUESTION, HOST, PEER)
    assert client.device.commands[1] == (HOST, "show interface eth1")
    assert v.steps[1]["args"] == {"interface": "eth1"}


@pytest.mark.parametrize("bad_decision, reason", [
    (call("show_everything"), "unknown tool"),
    (call("interface", interface="eth1; reload"), "not allowed"),
    ({"thought": "hmm", "action": "panic"}, "action must be call_tool or conclude"),
    (conclude("healthy"), "call at least one tool before concluding"),
])
def test_bad_decisions_are_rejected_and_fed_back(lab, scripted_llm, bad_decision, reason):
    client = lab("healthy")
    llm = scripted_llm(bad_decision, call("bgp_state"), conclude("healthy"))
    v = investigate(client, QUESTION, HOST, PEER)

    assert reason in v.steps[0]["rejected"]
    assert case_of(llm.calls[1])["steps"][0]["rejected"] == v.steps[0]["rejected"]
    assert client.device.commands == [(HOST, "show bgp summary")]   # nothing ran for the bad step
    assert v.resolved and v.checked == ["bgp_state"]
    assert "rejected" in v.pretty()


def test_bgp_state_must_come_first(lab, scripted_llm):
    client = lab("tcp_blocked")
    scripted_llm(call("tcp_port"), call("bgp_state"), call("tcp_port"), conclude("tcp_unreachable"))
    v = investigate(client, QUESTION, HOST, PEER)
    assert "call bgp_state first" in v.steps[0]["rejected"]
    assert v.checked == ["bgp_state", "tcp_port"]


def test_repeated_call_is_rejected(lab, scripted_llm):
    client = lab("neighbor_shutdown")
    scripted_llm(call("bgp_state"), call("bgp_state"), conclude("neighbor_shutdown"))
    v = investigate(client, QUESTION, HOST, PEER)
    assert "already called with these args in step 1" in v.steps[1]["rejected"]
    assert len(client.device.commands) == 1


def test_conclusion_without_full_diagnosis_is_rejected(lab, scripted_llm):
    incomplete = {"thought": "done", "action": "conclude", "diagnosis": {"resolved": True}}
    scripted_llm(call("bgp_state"), incomplete, conclude("healthy"))
    v = investigate(lab("healthy"), QUESTION, HOST, PEER)
    assert "missing root_cause" in v.steps[1]["rejected"]
    assert v.fault_class == "healthy"


def test_ml_classify_as_a_step(lab, scripted_llm):
    scripted_llm(call("bgp_state"), call("config"), call("ml_classify"), call("ml_classify"),
                 conclude("neighbor_shutdown"))
    v = investigate(lab("neighbor_shutdown"), QUESTION, HOST, PEER)
    assert v.steps[2]["summary"].startswith("neighbor_shutdown (")
    assert "already called" in v.steps[3]["rejected"]   # no new evidence since step 3
    assert v.checked == ["bgp_state", "config", "ml_classify"]
    assert v.ml_prediction["label"] == "neighbor_shutdown"   # second opinion ignores the ML step itself


def test_no_ml_hides_ml_classify(lab, scripted_llm):
    llm = scripted_llm(call("ml_classify"), call("bgp_state"), conclude("healthy"))
    v = investigate(lab("healthy"), QUESTION, HOST, PEER, use_ml=False)
    assert "ml_classify" not in {t["tool_id"] for t in case_of(llm.calls[0])["tools"]}
    assert "unknown tool" in v.steps[0]["rejected"]
    assert v.ml_prediction is None


def test_budget_forces_a_conclusion(lab, scripted_llm):
    llm = scripted_llm(call("bgp_state"), call("interface"),
                       conclude("other", "Not enough evidence", resolved=False,
                                confidence="low", next_checks=["show bgp neighbors 172.20.20.3"]))
    v = investigate(lab("remote_as_mismatch"), QUESTION, HOST, PEER, max_steps=2)

    assert "at most 2 more tool calls" in llm.calls[0]["user"]
    assert "at most 1 more tool call " in llm.calls[1]["user"]
    last = llm.calls[2]
    assert "You have used every tool call" in last["user"]
    assert last["schema"]["properties"]["action"]["enum"] == ["conclude"]
    assert (v.resolved, v.fault_class, v.next_checks) == (False, "other", ["show bgp neighbors 172.20.20.3"])


def test_raw_output_is_trimmed(lab, scripted_llm, monkeypatch):
    monkeypatch.setattr(agent, "RAW_OUTPUT_LIMIT", 100)
    llm = scripted_llm(call("bgp_state"), call("interface"), conclude("interface_down"))
    investigate(lab("interface_down"), QUESTION, HOST, PEER)
    raw = case_of(llm.calls[2])["steps"][1]["result"]["raw_output"]
    assert raw.startswith("Interface eth0 is up") and "more characters cut" in raw


def test_all_tools_failing_is_evidence(lab, scripted_llm):
    scripted_llm(call("bgp_state"), conclude("device_unreachable", "SSH to the router fails"))
    v = investigate(lab("device_unreachable"), QUESTION, HOST, PEER)
    assert v.steps[0]["summary"].startswith("failed: NoValidConnectionsError")
    assert (v.resolved, v.fault_class) == (True, "device_unreachable")


# --- falling back to the rule chain ---

def test_llm_down_at_start_falls_back_to_rules(lab, scripted_llm):
    scripted_llm(LLMUnavailable("could not reach Ollama"))
    v = investigate(lab("interface_down"), QUESTION, HOST, PEER)
    assert (v.source, v.root_cause) == ("rules", "Interface(s) down: eth1")
    assert v.notes == ["LLM agent unavailable (could not reach Ollama); fell back to the rule chain"]


def test_llm_down_midway_falls_back_to_rules(lab, scripted_llm):
    scripted_llm(call("bgp_state"), LLMUnavailable("Ollama did not answer within 300s"))
    v = investigate(lab("neighbor_shutdown"), QUESTION, HOST, PEER)
    assert v.source == "ml"
    assert v.notes[-1].startswith("LLM agent unavailable after 1 step(s)")


def test_unusable_forced_conclusion_falls_back(lab, scripted_llm):
    bad_final = {"thought": "?", "action": "conclude", "diagnosis": {"resolved": False}}
    scripted_llm(call("bgp_state"), bad_final)
    v = investigate(lab("healthy"), QUESTION, HOST, PEER, max_steps=1)
    assert v.source == "rules"
    assert "conclusion is missing" in v.notes[-1]


# --- CLI ---

def run_cli(monkeypatch, tmp_path, *flags):
    seen = {}
    monkeypatch.setattr("analyzer.run.investigate", lambda *a, **kw: seen.update(mode="agent", **kw) or _stub())
    monkeypatch.setattr("analyzer.run.diagnose", lambda *a, **kw: seen.update(mode="rules", **kw) or _stub())
    main([QUESTION, "--host", HOST, "--peer", PEER, "--log-dir", str(tmp_path), *flags])
    return seen


def _stub():
    from analyzer.verdict import Verdict
    return Verdict(True, "x", "y")


def test_cli_defaults_to_agent(monkeypatch, tmp_path):
    seen = run_cli(monkeypatch, tmp_path)
    assert seen == {"mode": "agent", "max_steps": 6, "use_ml": True, "trust_rules": False}


def test_cli_agent_flags(monkeypatch, tmp_path):
    seen = run_cli(monkeypatch, tmp_path, "--max-steps", "3", "--trust-rules", "--no-ml")
    assert seen == {"mode": "agent", "max_steps": 3, "use_ml": False, "trust_rules": True}


@pytest.mark.parametrize("flags", [("--mode", "rules"), ("--no-llm",)])
def test_cli_rules_mode(monkeypatch, tmp_path, flags):
    seen = run_cli(monkeypatch, tmp_path, *flags)
    assert seen["mode"] == "rules"
    assert seen["use_llm"] is ("--no-llm" not in flags)
