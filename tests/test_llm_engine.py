"""Unit tests for the LLM-assisted diagnosis layer, EvidenceBuilder, and LLMEngine."""
import json
import os
import pytest
from unittest.mock import MagicMock, patch

from analyzer.llm_engine import EvidenceBuilder, LLMDiagnosis, LLMEngine
from analyzer.rules_engine import diagnose
from analyzer.verdict import Verdict
from tests.test_analyzer import MockRestClient


# ==================================================
# 1. EVIDENCE BUILDER & SANITIZATION TESTS
# ==================================================

def test_evidence_builder_sanitization_removes_secrets():
    """Ensure no passwords, tokens, API keys, or credentials pass into LLM context."""
    raw_evidence = [
        {
            "tool": "config",
            "parsed": {
                "password": "supersecretpassword123",
                "api_key": "secret_key_abc",
                "auth_token": "token_xyz",
                "safe_field": "safe_value",
                "nested": {
                    "credential": "admin_password",
                    "router_id": "1.1.1.1",
                },
            },
        }
    ]
    context = EvidenceBuilder.build_context(
        question="Why is BGP down?",
        host="router1",
        peer="172.20.20.3",
        starting_intent="bgp_state_check",
        checked=["config"],
        evidence=raw_evidence,
        deterministic_result={"resolved": True, "root_cause": "Config error"},
    )
    context_str = json.dumps(context)
    assert "supersecretpassword123" not in context_str
    assert "secret_key_abc" not in context_str
    assert "token_xyz" not in context_str
    assert "admin_password" not in context_str
    assert context["evidence"]["config"]["safe_field"] == "safe_value"
    assert context["evidence"]["config"]["nested"]["router_id"] == "1.1.1.1"


def test_evidence_builder_route_summarization_and_truncation():
    """Test route truncation and explicit truncation disclaimer when > 10 routes."""
    # Generate 15 sample routes
    routes_map = {}
    for i in range(15):
        prefix = f"10.0.{i}.0/24"
        routes_map[prefix] = [
            {
                "prefix": prefix,
                "valid": True,
                "bestpath": (i % 2 == 0),
                "peer": "172.20.20.3" if i == 5 else "192.168.1.1",
                "as_path": f"6500{i}",
                "next_hops": ["172.20.20.3"] if i == 5 else ["10.0.0.1"],
                "metric": 100,
            }
        ]

    summary = EvidenceBuilder.prepare_routes_summary({"routes": routes_map}, peer_ip="172.20.20.3")

    assert summary["total_prefixes"] == 15
    assert len(summary["routes_sample"]) == 10
    assert summary["truncated"] is True
    assert "Route telemetry was truncated; conclusions about routes are limited to the provided subset." in summary["truncation_notice"]

    # Peer matching route (prefix 10.0.5.0/24) must be prioritized in sample
    sampled_prefixes = [r["prefix"] for r in summary["routes_sample"]]
    assert "10.0.5.0/24" in sampled_prefixes


def test_evidence_builder_small_route_table_not_truncated():
    """Test route table with <= 10 routes is not truncated."""
    routes_map = {
        "10.0.1.0/24": [{"prefix": "10.0.1.0/24", "valid": True, "bestpath": True, "peer": "172.20.20.3"}],
        "10.0.2.0/24": [{"prefix": "10.0.2.0/24", "valid": True, "bestpath": True, "peer": "172.20.20.3"}],
    }
    summary = EvidenceBuilder.prepare_routes_summary({"routes": routes_map}, peer_ip="172.20.20.3")
    assert summary["total_prefixes"] == 2
    assert summary["truncated"] is False
    assert "truncation_notice" not in summary


# ==================================================
# 2. LLM DIAGNOSIS STRUCTURE & PARSING TESTS
# ==================================================

def test_llm_diagnosis_from_dict_and_normalization():
    data = {
        "summary": "Interface eth1 is down causing BGP session failure.",
        "diagnosis": "Physical link down on eth1.",
        "confidence": "HIGH",
        "confirmed_findings": ["Interface eth1 is link_state down", "BGP is Active"],
        "likely_causes": ["Physical link failure", "Remote interface shut"],
        "evidence": ["eth1 link_state: down"],
        "missing_evidence": ["Remote switch syslogs"],
        "recommended_actions": ["Check cable / remote peer port status"],
        "explanation": "Because eth1 is down, TCP port 179 packets cannot transit.",
    }
    diag = LLMDiagnosis.from_dict(data)
    assert diag.confidence == "high"
    assert diag.status == "success"
    assert len(diag.confirmed_findings) == 2
    assert "Physical link down" in diag.diagnosis

    # Pretty output test
    pretty_text = diag.pretty()
    assert "LLM-ASSISTED DIAGNOSIS & EXPLANATION" in pretty_text
    assert "Confidence:  HIGH" in pretty_text
    assert "Interface eth1 is link_state down" in pretty_text


def test_llm_diagnosis_malformed_response():
    """Malformed non-dict object handled cleanly."""
    diag = LLMDiagnosis.from_dict("not a dict")
    assert diag.status == "error"
    assert diag.confidence == "low"
    assert "Invalid LLM response format" in diag.summary


# ==================================================
# 3. LLM ENGINE PROVIDER MOCKING & FALLBACK TESTS
# ==================================================

def test_llm_engine_disabled_by_default_without_credentials(monkeypatch):
    """When disabled / no API key, engine returns safe disabled status."""
    for key in ["LLM_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY", "LLM_ENABLED"]:
        monkeypatch.delenv(key, raising=False)
    engine = LLMEngine(api_key=None, enabled=False)
    assert engine.is_available() is False
    res = engine.diagnose({"question": "Why is BGP down?"})
    assert res.status == "disabled"
    assert "disabled" in res.summary.lower()


def test_llm_engine_missing_api_key_status_unavailable(monkeypatch):
    """When enabled but no API key or client provided, returns status unavailable."""
    for key in ["LLM_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "OPENAI_API_KEY"]:
        monkeypatch.delenv(key, raising=False)
    engine = LLMEngine(api_key=None, enabled=True)
    assert engine.is_available() is False
    res = engine.diagnose({"question": "Why is BGP down?"})
    assert res.status == "unavailable"
    assert "unavailable" in res.summary.lower()


def test_llm_engine_successful_custom_client_diagnosis():
    """Custom client mock receives prompt and produces valid structured diagnosis."""
    mock_response = json.dumps({
        "summary": "BGP is down due to TCP port 179 block.",
        "diagnosis": "Transport layer unreachable.",
        "confidence": "high",
        "confirmed_findings": ["TCP port 179 unreachable", "Interface is up"],
        "likely_causes": ["Firewall/ACL drop", "Remote peer BGP process not running"],
        "evidence": ["TCP port 179: unreachable"],
        "missing_evidence": ["ACL configuration on intermediate path"],
        "recommended_actions": ["Verify ACL permits TCP port 179"],
        "explanation": "The BGP session cannot progress past Connect/Active state without TCP.",
    })

    def mock_client(prompt, system_instruction):
        assert "User Question: Why is BGP down?" in prompt
        assert "TCP port 179" in prompt
        return mock_response

    engine = LLMEngine(client=mock_client, enabled=True)
    assert engine.is_available() is True
    res = engine.diagnose({
        "question": "Why is BGP down?",
        "host": "router1",
        "peer": "172.20.20.3",
        "deterministic_result": {"resolved": True, "root_cause": "TCP port 179 unreachable"},
        "evidence": {"tcp_port": {"reachable": False}},
    })

    assert res.status == "success"
    assert res.confidence == "high"
    assert res.diagnosis == "Transport layer unreachable."
    assert "Firewall/ACL drop" in res.likely_causes


def test_llm_engine_markdown_wrapped_json_parsing():
    """Ensure ```json ``` code fences are cleanly stripped."""
    raw_output = """```json
    {
        "summary": "Config drift detected.",
        "diagnosis": "Neighbor remote-as mismatch.",
        "confidence": "high",
        "confirmed_findings": ["Config drifted from baseline"],
        "likely_causes": ["Manual configuration change"],
        "evidence": ["Diff shows deleted neighbor"],
        "missing_evidence": [],
        "recommended_actions": ["Restore remote-as setting"],
        "explanation": "BGP configuration was modified."
    }
    ```"""

    engine = LLMEngine(client=lambda p, s: raw_output, enabled=True)
    res = engine.diagnose({"question": "Why is BGP down?"})
    assert res.status == "success"
    assert res.diagnosis == "Neighbor remote-as mismatch."


def test_llm_engine_malformed_json_fallback():
    """Ensure malformed JSON from LLM does not raise an exception and returns error status."""
    engine = LLMEngine(client=lambda p, s: "This is not JSON at all!", enabled=True)
    res = engine.diagnose({"question": "Why is BGP down?"})
    assert res.status == "error"
    assert "Malformed JSON" in res.error
    assert "relying on deterministic result" in res.summary


def test_llm_engine_timeout_fallback():
    """Ensure timeout from provider is caught and handled safely."""
    def timeout_client(p, s):
        raise TimeoutError("HTTP request timed out after 10.0s")

    engine = LLMEngine(client=timeout_client, enabled=True)
    res = engine.diagnose({"question": "Why is BGP down?"})
    assert res.status == "error"
    assert "timed out" in res.error
    assert res.confidence == "low"


def test_llm_engine_exception_does_not_leak_api_key():
    """Ensure API key is not included in error messages or logs."""
    secret_key = "AIzaSySecretApiKey12345"

    def failing_client(p, s):
        raise RuntimeError(f"Connection failed for key {secret_key}")

    engine = LLMEngine(api_key=secret_key, client=failing_client, enabled=True)
    res = engine.diagnose({"question": "Why is BGP down?"})
    assert secret_key not in res.error
    assert "[REDACTED_API_KEY]" in res.error


# ==================================================
# 4. INTEGRATION TESTS WITH RULES ENGINE & VERDICT
# ==================================================

def test_diagnose_integration_root_cause_with_llm():
    """Deterministic root cause found and enriched with LLM explanation."""
    client = MockRestClient({
        "/tools/bgp/state": {
            "parsed": {"queried_peer_state": "Active", "peers": {"172.20.20.3": "Active"}},
            "success": True,
        },
        "/tools/interface/detail": {
            "parsed": {
                "interfaces": {
                    "eth1": {"link_state": "up", "admin_state": "up", "ip_address": "172.20.20.2/24"}
                }
            },
            "success": True,
        },
        "/tools/tcp/check": {
            "parsed": {"peer_ip": "172.20.20.3", "port": 179, "reachable": False},
            "success": True,
        },
    })

    mock_llm_json = json.dumps({
        "summary": "BGP session down due to TCP port 179 unreachable.",
        "diagnosis": "TCP transport failure to 172.20.20.3 on port 179.",
        "confidence": "high",
        "confirmed_findings": [
            "Interface eth1 is operational (UP)",
            "TCP port 179 to 172.20.20.3 is unreachable",
            "BGP state is Active",
        ],
        "likely_causes": ["Security group / ACL filtering port 179", "Routing blackhole"],
        "evidence": ["Port 179 to 172.20.20.3 is unreachable"],
        "missing_evidence": ["Firewall logs between router1 and 172.20.20.3"],
        "recommended_actions": ["Inspect network ACLs allowing TCP 179"],
        "explanation": "BGP relies on a standard TCP 3-way handshake on port 179.",
    })

    llm_engine = LLMEngine(client=lambda p, s: mock_llm_json, enabled=True)

    verdict = diagnose(
        client,
        "Why is BGP down?",
        "172.20.20.2",
        "172.20.20.3",
        llm_engine=llm_engine,
    )

    # 1. Deterministic result remains authoritative
    assert verdict.resolved is True
    assert "TCP port 179" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface", "tcp_port"]

    # 2. LLM explanation is attached
    assert verdict.llm_diagnosis is not None
    assert verdict.llm_diagnosis.status == "success"
    assert verdict.llm_diagnosis.confidence == "high"
    assert "Security group / ACL filtering port 179" in verdict.llm_diagnosis.likely_causes

    # 3. Verdict pretty output contains both deterministic and LLM sections
    pretty = verdict.pretty()
    assert "ROOT CAUSE FOUND" in pretty
    assert "TCP port 179 to 172.20.20.3 is not reachable" in pretty
    assert "LLM-ASSISTED DIAGNOSIS & EXPLANATION" in pretty
    assert "Confidence:  HIGH" in pretty
    assert "Security group / ACL filtering port 179" in pretty


def test_diagnose_integration_unresolved_with_llm():
    """Unresolved deterministic case enriched with LLM analysis of what was ruled out."""
    client = MockRestClient({
        "/tools/bgp/state": {
            "parsed": {"queried_peer_state": "Active", "peers": {"172.20.20.3": "Active"}},
            "success": True,
        },
        "/tools/interface/detail": {
            "parsed": {
                "interfaces": {
                    "eth1": {"link_state": "up", "admin_state": "up", "ip_address": "172.20.20.2/24"}
                }
            },
            "success": True,
        },
        "/tools/tcp/check": {
            "parsed": {"peer_ip": "172.20.20.3", "port": 179, "reachable": True},
            "success": True,
        },
        "/tools/config/diff": {
            "parsed": {"has_baseline": True, "diff": [], "drifted": False},
            "success": True,
        },
    })

    mock_llm_json = json.dumps({
        "summary": "Interface, TCP, and config baseline match, but BGP peer is Active.",
        "diagnosis": "BGP session-level negotiation or peer configuration mismatch.",
        "confidence": "medium",
        "confirmed_findings": [
            "Interface eth1 is UP",
            "TCP port 179 is REACHABLE",
            "Local config matches baseline",
            "BGP state is stuck in Active",
        ],
        "likely_causes": [
            "Remote peer AS number mismatch",
            "BGP MD5 password mismatch on remote peer",
            "Remote peer is not configured to peer with 172.20.20.2",
        ],
        "evidence": ["Interface: up", "TCP 179: reachable", "Config: no drift"],
        "missing_evidence": ["Remote peer configuration", "BGP notification messages / logs"],
        "recommended_actions": [
            "Verify remote peer ASN and MD5 password configuration",
            "Check show bgp neighbors on remote router",
        ],
        "explanation": "Since layer 1-4 connectivity and local configuration are verified healthy, the fault is likely in protocol negotiation.",
    })

    llm_engine = LLMEngine(client=lambda p, s: mock_llm_json, enabled=True)

    verdict = diagnose(
        client,
        "Why is BGP down?",
        "172.20.20.2",
        "172.20.20.3",
        llm_engine=llm_engine,
    )

    # 1. Deterministic Verdict is unresolved
    assert verdict.resolved is False
    assert "No root cause found" in verdict.root_cause
    assert len(verdict.checked) == 4

    # 2. LLM enriches with ruled-out insights & missing evidence
    assert verdict.llm_diagnosis is not None
    assert verdict.llm_diagnosis.confidence == "medium"
    assert "Remote peer AS number mismatch" in verdict.llm_diagnosis.likely_causes
    assert "Remote peer configuration" in verdict.llm_diagnosis.missing_evidence

    pretty = verdict.pretty()
    assert "NO ROOT CAUSE FOUND" in pretty
    assert "LLM-ASSISTED DIAGNOSIS & EXPLANATION" in pretty
    assert "Remote peer AS number mismatch" in pretty


def test_diagnose_integration_llm_failure_does_not_break_verdict():
    """If LLM call fails with exception, deterministic verdict is returned intact."""
    client = MockRestClient({
        "/tools/bgp/state": {
            "parsed": {"queried_peer_state": "Active", "peers": {"172.20.20.3": "Active"}},
            "success": True,
        },
        "/tools/interface/detail": {
            "parsed": {
                "interfaces": {
                    "eth1": {"link_state": "down", "admin_state": "up", "ip_address": "172.20.20.2/24"}
                }
            },
            "success": True,
        },
    })

    def throwing_client(p, s):
        raise ConnectionResetError("Remote API dropped connection")

    llm_engine = LLMEngine(client=throwing_client, enabled=True)

    verdict = diagnose(
        client,
        "Why is BGP down?",
        "172.20.20.2",
        "172.20.20.3",
        llm_engine=llm_engine,
    )

    # Deterministic verdict is 100% intact
    assert verdict.resolved is True
    assert "eth1" in verdict.root_cause
    assert verdict.llm_diagnosis is not None
    assert verdict.llm_diagnosis.status == "error"
    assert "Remote API dropped connection" in verdict.llm_diagnosis.error

    pretty = verdict.pretty()
    assert "ROOT CAUSE FOUND" in pretty
    assert "Relevant interface eth1" in pretty
    assert "Status:      ERROR" in pretty


def test_evidence_builder_with_full_telemetry():
    """Test EvidenceBuilder correctly integrates deep telemetry (neighbors & routes)."""
    telemetry = {
        "neighbors": {
            "parsed": {
                "neighbors": {
                    "172.20.20.3": {
                        "peer": "172.20.20.3",
                        "remote_as": 65002,
                        "local_as": 65001,
                        "bgp_state": "Active",
                        "hold_time": 9,
                        "keepalive_time": 3,
                        "prefix_received_count": 0,
                    }
                }
            }
        },
        "routes": {
            "parsed": {
                "routes": {
                    "10.0.1.0/24": [{"prefix": "10.0.1.0/24", "valid": True, "bestpath": True, "peer": "172.20.20.3"}]
                }
            }
        },
    }

    context = EvidenceBuilder.build_context(
        question="Why is BGP down?",
        host="router1",
        peer="172.20.20.3",
        starting_intent="bgp_state_check",
        checked=["bgp_state", "interface", "tcp_port", "config"],
        evidence=[{"tool": "bgp_state", "parsed": {"queried_peer_state": "Active"}}],
        deterministic_result={"resolved": False, "root_cause": "No root cause"},
        telemetry=telemetry,
    )

    assert "bgp_neighbors" in context["evidence"]
    assert context["evidence"]["bgp_neighbors"]["172.20.20.3"]["remote_as"] == 65002
    assert "bgp_routes" in context["evidence"]
    assert context["evidence"]["bgp_routes"]["total_prefixes"] == 1


def test_llm_engine_extract_json_with_surrounding_commentary():
    """Ensure LLM responses that contain conversational text around JSON are parsed."""
    noisy_output = """Here is the diagnostic result:
    {
        "summary": "BGP session is failing due to remote ASN mismatch.",
        "diagnosis": "ASN configuration mismatch between peers.",
        "confidence": "high",
        "confirmed_findings": ["Peer AS is 65002"],
        "likely_causes": ["Configured peer AS mismatch"],
        "evidence": ["Remote AS: 65002"],
        "missing_evidence": [],
        "recommended_actions": ["Correct neighbor AS in config"],
        "explanation": "The remote peer sent an OPEN message with AS 65002."
    }
    Hope this helps!"""

    engine = LLMEngine(client=lambda p, s: noisy_output, enabled=True)
    res = engine.diagnose({"question": "Why is BGP down?"})
    assert res.status == "success"
    assert res.confidence == "high"
    assert "ASN configuration mismatch" in res.diagnosis
