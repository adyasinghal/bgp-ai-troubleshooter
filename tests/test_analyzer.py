"""Unit and integration tests for reasoning engine, evaluation, and Verdict presentation."""
import pytest
from unittest.mock import MagicMock

from analyzer.rules_engine import _evaluate, _is_peer_on_interface, diagnose
from analyzer.verdict import Verdict


class MockRestClient:
    """Mock client simulating the Rules DB and Tool Cohort REST API."""

    def __init__(self, tool_responses: dict | None = None):
        self.tool_responses = tool_responses or {}
        self.called_endpoints = []
        self.rules = {
            "bgp_state_check": {
                "intent": "bgp_state_check",
                "tools": [{"tool_id": "bgp_state", "endpoint": "/tools/bgp/state", "base_command": "show bgp summary"}],
                "next_intent_on_fail": "interface_check",
            },
            "interface_check": {
                "intent": "interface_check",
                "tools": [{"tool_id": "interface", "endpoint": "/tools/interface/detail", "base_command": "show interface detail"}],
                "next_intent_on_fail": "tcp_port_check",
            },
            "tcp_port_check": {
                "intent": "tcp_port_check",
                "tools": [{"tool_id": "tcp_port", "endpoint": "/tools/tcp/check", "base_command": "check port 179"}],
                "next_intent_on_fail": "config_check",
            },
            "config_check": {
                "intent": "config_check",
                "tools": [{"tool_id": "config", "endpoint": "/tools/config/diff", "base_command": "show run diff"}],
                "next_intent_on_fail": None,
            },
        }

    def get_rule(self, intent: str) -> dict:
        return self.rules.get(intent, {})

    def call_tool(self, endpoint: str, payload: dict) -> dict:
        self.called_endpoints.append((endpoint, payload))
        if endpoint in self.tool_responses:
            res = self.tool_responses[endpoint]
            if isinstance(res, Exception):
                raise res
            return res
        return {"parsed": {}, "success": True}


# ==================================================
# 10 REQUIRED REASONING PATH TESTS
# ==================================================

def test_1_bgp_established_healthy():
    """TEST 1: BGP Established -> healthy -> stops."""
    client = MockRestClient({
        "/tools/bgp/state": {
            "parsed": {"queried_peer_state": "Established", "peers": {"172.20.20.3": "Established"}},
            "success": True,
        }
    })
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "Established" in verdict.root_cause
    assert verdict.checked == ["bgp_state"]
    assert len(client.called_endpoints) == 1


def test_2_all_healthy_unresolved():
    """TEST 2: BGP Active -> interface healthy -> TCP reachable -> config healthy -> unresolved."""
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
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is False
    assert "No root cause found" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface", "tcp_port", "config"]


def test_3_bgp_active_relevant_interface_down():
    """TEST 3: BGP Active -> interface relevant to peer is down -> root cause interface."""
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
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "eth1" in verdict.root_cause
    assert "down" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface"]
    assert len(client.called_endpoints) == 2


def test_4_bgp_active_tcp_unreachable():
    """TEST 4: BGP Active -> interface healthy -> TCP unreachable -> root cause TCP."""
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
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "TCP port 179" in verdict.root_cause
    assert "not reachable" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface", "tcp_port"]


def test_5_bgp_active_config_drift():
    """TEST 5: BGP Active -> interface healthy -> TCP reachable -> config drift -> root cause config."""
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
            "parsed": {"has_baseline": True, "diff": ["- neighbor 172.20.20.3 remote-as 65002"], "drifted": True},
            "success": True,
        },
    })
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "drifted" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface", "tcp_port", "config"]


def test_6_user_asks_interface_starts_at_interface():
    """TEST 6: User asks interface -> starts at interface -> does NOT run BGP first."""
    client = MockRestClient({
        "/tools/interface/detail": {
            "parsed": {
                "interfaces": {
                    "eth1": {"link_state": "down", "admin_state": "up", "ip_address": "172.20.20.2/24"}
                }
            },
            "success": True,
        },
    })
    verdict = diagnose(client, "Why is interface eth1 down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "eth1" in verdict.root_cause
    assert "bgp_state" not in verdict.checked
    assert verdict.checked == ["interface"]


def test_7_user_asks_tcp_starts_at_tcp():
    """TEST 7: User asks TCP -> starts at TCP -> may continue to config -> does NOT run BGP/interface first."""
    client = MockRestClient({
        "/tools/tcp/check": {
            "parsed": {"peer_ip": "172.20.20.3", "port": 179, "reachable": True},
            "success": True,
        },
        "/tools/config/diff": {
            "parsed": {"has_baseline": True, "diff": ["- remote-as 65002"], "drifted": True},
            "success": True,
        },
    })
    verdict = diagnose(client, "Is BGP port 179 reachable?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "drifted" in verdict.root_cause
    assert "bgp_state" not in verdict.checked
    assert "interface" not in verdict.checked
    assert verdict.checked == ["tcp_port", "config"]


def test_8_user_asks_config_only_starts_at_config():
    """TEST 8: User asks config -> only starts at config."""
    client = MockRestClient({
        "/tools/config/diff": {
            "parsed": {"has_baseline": True, "diff": ["- diff line"], "drifted": True},
            "success": True,
        },
    })
    verdict = diagnose(client, "Did the configuration change?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "drifted" in verdict.root_cause
    assert verdict.checked == ["config"]
    assert len(client.called_endpoints) == 1


def test_9_unrelated_interface_down_not_falsely_blamed():
    """TEST 9: Unrelated interface is down -> do not incorrectly report it as root cause."""
    client = MockRestClient({
        "/tools/bgp/state": {
            "parsed": {"queried_peer_state": "Active", "peers": {"172.20.20.3": "Active"}},
            "success": True,
        },
        "/tools/interface/detail": {
            "parsed": {
                "interfaces": {
                    # eth1 is relevant to peer 172.20.20.3 (subnet 172.20.20.0/24) and is UP
                    "eth1": {"link_state": "up", "admin_state": "up", "ip_address": "172.20.20.2/24"},
                    # eth2 is UNRELATED and is DOWN
                    "eth2": {"link_state": "down", "admin_state": "down", "ip_address": "10.99.99.1/24"},
                }
            },
            "success": True,
        },
        "/tools/tcp/check": {
            "parsed": {"peer_ip": "172.20.20.3", "port": 179, "reachable": False},
            "success": True,
        },
    })
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    # It must NOT stop at eth2! It should continue to tcp_port and identify TCP unreachable as root cause.
    assert verdict.resolved is True
    assert "TCP port 179" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface", "tcp_port"]


def test_10_missing_config_baseline_no_false_drift():
    """TEST 10: Missing config baseline -> do not report false config drift."""
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
            # Baseline missing -> has_baseline: False, drifted: False
            "parsed": {"has_baseline": False, "diff": [], "drifted": False},
            "success": True,
        },
    })
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is False
    assert "No root cause found" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface", "tcp_port", "config"]


# ==================================================
# INTERFACE EVALUATION & HELPER TESTS
# ==================================================

def test_is_peer_on_interface():
    assert _is_peer_on_interface("172.20.20.3", "172.20.20.2/24") is True
    assert _is_peer_on_interface("10.0.1.2", "10.0.1.1/24") is True
    assert _is_peer_on_interface("192.168.1.100", "172.20.20.2/24") is False
    assert _is_peer_on_interface(None, "172.20.20.2/24") is False
    assert _is_peer_on_interface("172.20.20.3", None) is False
    assert _is_peer_on_interface("invalid-ip", "172.20.20.2/24") is False


def test_evaluate_interface_admin_down():
    result = {
        "parsed": {
            "interfaces": {
                "eth1": {"link_state": "down", "admin_state": "down", "ip_address": "172.20.20.2/24"}
            }
        }
    }
    outcome = _evaluate("interface", result, {"host": "router1", "peer": "172.20.20.3"})
    assert outcome["decision"] == "root_cause"
    assert "administratively down" in outcome["cause"]


def test_evaluate_interface_insufficient_info():
    result = {
        "parsed": {
            "interfaces": {
                # eth1 has no IP address, cannot determine relevance
                "eth1": {"link_state": "down", "admin_state": "up", "ip_address": None}
            }
        }
    }
    outcome = _evaluate("interface", result, {"host": "router1", "peer": "172.20.20.3"})
    # Prefer continue over false positive
    assert outcome["decision"] == "continue"


# ==================================================
# ERROR HANDLING & RESILIENCE TESTS
# ==================================================

def test_diagnose_tool_call_exception_handling():
    """If a tool raises an HTTP/network exception, continue chain conservatively."""
    client = MockRestClient({
        "/tools/bgp/state": Exception("Connection refused to tool service"),
        "/tools/interface/detail": {
            "parsed": {
                "interfaces": {
                    "eth1": {"link_state": "down", "admin_state": "up", "ip_address": "172.20.20.2/24"}
                }
            },
            "success": True,
        },
    })
    verdict = diagnose(client, "Why is BGP down?", "172.20.20.2", "172.20.20.3")
    assert verdict.resolved is True
    assert "eth1" in verdict.root_cause
    assert verdict.checked == ["bgp_state", "interface"]


def test_evaluate_malformed_result():
    assert _evaluate("bgp_state", {}, {}) == {"decision": "continue"}
    assert _evaluate("bgp_state", {"parsed": None}, {}) == {"decision": "continue"}
    assert _evaluate("interface", {"parsed": "invalid"}, {}) == {"decision": "continue"}
    assert _evaluate("tcp_port", {"parsed": {}}, {}) == {"decision": "continue"}
    assert _evaluate("config", {"parsed": {"drifted": True, "has_baseline": False}}, {}) == {"decision": "continue"}


# ==================================================
# VERDICT FORMATTING TESTS
# ==================================================

def test_verdict_pretty_healthy():
    v = Verdict(
        resolved=True,
        root_cause="BGP peer 172.20.20.3 is Established",
        suggested_fix="No action needed — the session is up.",
        checked=["bgp_state"],
        evidence=[{"tool": "bgp_state", "parsed": {"queried_peer_state": "Established"}}],
    )
    p = v.pretty()
    assert "HEALTHY (NO FAULT DETECTED)" in p
    assert "BGP peer 172.20.20.3 is Established" in p
    assert "✓ BGP state" in p
    assert "Peer state: Established" in p


def test_verdict_pretty_root_cause():
    v = Verdict(
        resolved=True,
        root_cause="TCP port 179 to 172.20.20.3 is not reachable",
        suggested_fix="Check routing/ACLs so 172.20.20.3 is reachable on port 179.",
        checked=["bgp_state", "interface", "tcp_port"],
        evidence=[
            {"tool": "bgp_state", "parsed": {"queried_peer_state": "Active"}},
            {"tool": "interface", "parsed": {"interfaces": {"eth1": {"link_state": "up", "admin_state": "up", "ip_address": "172.20.20.2/24"}}}},
            {"tool": "tcp_port", "parsed": {"peer_ip": "172.20.20.3", "port": 179, "reachable": False}},
        ],
    )
    p = v.pretty()
    assert "ROOT CAUSE FOUND" in p
    assert "TCP port 179 to 172.20.20.3 is not reachable" in p
    assert "✓ BGP state" in p
    assert "✓ Interface state" in p
    assert "✗ TCP port 179" in p
    assert "Port 179 to 172.20.20.3 is unreachable" in p


def test_verdict_pretty_unresolved():
    v = Verdict(
        resolved=False,
        root_cause="No root cause found by the rule chain.",
        suggested_fix="Escalate to a human or LLM.",
        checked=["bgp_state", "interface", "tcp_port", "config"],
        evidence=[
            {"tool": "bgp_state", "parsed": {"queried_peer_state": "Active"}},
            {"tool": "interface", "parsed": {"interfaces": {"eth1": {"link_state": "up", "admin_state": "up"}}}},
            {"tool": "tcp_port", "parsed": {"peer_ip": "172.20.20.3", "port": 179, "reachable": True}},
            {"tool": "config", "parsed": {"has_baseline": True, "drifted": False, "diff": []}},
        ],
    )
    p = v.pretty()
    assert "NO ROOT CAUSE FOUND" in p
    assert "✓ BGP state" in p
    assert "✓ Interface state" in p
    assert "✓ TCP port 179" in p
    assert "✓ Configuration drift" in p
    assert "Config matches baseline (no drift)" in p
