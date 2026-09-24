"""
Demonstration Script for LLM-Assisted Diagnosis & Fallback.

This script demonstrates:
1. Complete deterministic reasoning + LLM-assisted diagnosis & explanation.
2. Safe fallback when LLM is unavailable / unconfigured.
3. Authority of deterministic Verdict preserved in all cases.
"""

import json
import sys

# Ensure UTF-8 stdout on Windows console
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from analyzer.llm_engine import LLMEngine
from analyzer.rules_engine import diagnose
from tests.test_analyzer import MockRestClient


def run_llm_enabled_demo():
    print("=" * 60)
    print("DEMONSTRATION 1: DETERMINISTIC ROOT CAUSE + LLM EXPLANATION")
    print("=" * 60)

    # 1. Simulate REST tool responses (TCP 179 unreachable)
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

    # 2. Mock structured LLM response
    mock_llm_response = json.dumps({
        "summary": "BGP session is failing to establish because TCP port 179 is unreachable on peer 172.20.20.3.",
        "diagnosis": "Layer 4 transport blockage preventing BGP 3-way handshake.",
        "confidence": "high",
        "confirmed_findings": [
            "Local interface eth1 is UP with IP 172.20.20.2/24.",
            "BGP neighbor state is Active (waiting for TCP connection).",
            "TCP probe to 172.20.20.3:179 returned UNREACHABLE.",
        ],
        "likely_causes": [
            "Firewall / ACL blocking TCP port 179 inbound on peer or transit switch.",
            "BGP routing daemon on peer 172.20.20.3 is not running or not listening on port 179.",
            "Missing return route on remote peer back to 172.20.20.2.",
        ],
        "evidence": [
            "BGP: Active",
            "eth1: link up, admin up",
            "TCP 179: unreachable",
        ],
        "missing_evidence": [
            "Remote peer router configuration and process status.",
            "Intermediate ACL / firewall drop counters.",
        ],
        "recommended_actions": [
            "Verify ACL rules allow TCP traffic on port 179 between 172.20.20.2 and 172.20.20.3.",
            "Confirm BGP daemon is active on peer 172.20.20.3.",
            "Test ICMP ping or traceroute to 172.20.20.3 to confirm IP reachability.",
        ],
        "explanation": "The BGP state machine requires an underlying TCP connection on port 179. Because the local interface is operational, the failure is isolated to transport reachability or remote peer listener state.",
    })

    # Mock engine
    llm_engine = LLMEngine(client=lambda p, s: mock_llm_response, enabled=True)

    verdict = diagnose(
        client=client,
        question="Why is BGP down on Router1?",
        host="router1",
        peer="172.20.20.3",
        llm_engine=llm_engine,
    )

    print(verdict.pretty())
    print("\n")


def run_llm_fallback_demo():
    print("=" * 60)
    print("DEMONSTRATION 2: SAFE FALLBACK (LLM UNAVAILABLE / DISABLED)")
    print("=" * 60)

    # 1. Simulate REST tool responses (Config drift)
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
            "parsed": {
                "has_baseline": True,
                "diff": ["- neighbor 172.20.20.3 remote-as 65002"],
                "drifted": True,
            },
            "success": True,
        },
    })

    # LLM engine with no key / disabled
    llm_engine = LLMEngine(api_key=None, enabled=True)

    verdict = diagnose(
        client=client,
        question="Why is BGP down?",
        host="router1",
        peer="172.20.20.3",
        llm_engine=llm_engine,
    )

    print(verdict.pretty())
    print("\n")


if __name__ == "__main__":
    run_llm_enabled_demo()
    run_llm_fallback_demo()
