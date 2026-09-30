"""Tool registry: built from the API's catalog; validates and runs tool calls."""
import json

import pytest
import requests

from analyzer.rules_engine import evaluate
from analyzer.tool_registry import ToolCallError, ToolRegistry
from tests.scenarios import HOST, PEER

CTX = {"host": HOST, "peer": PEER}


@pytest.fixture
def registry(lab):
    return ToolRegistry.load(lab("healthy"))


def test_catalog_endpoint_not_shadowed_by_intent_route(lab):
    catalog = lab("healthy").get_catalog()
    assert {t["tool_id"] for t in catalog["tools"]} == {"bgp_state", "interface", "tcp_port", "config", "ml_classify"}
    assert len(catalog["rules"]) == 4


def test_for_prompt_is_compact_and_serializable(registry):
    entries = {t["tool_id"]: t for t in registry.for_prompt()}
    assert set(entries["interface"]) == {"tool_id", "description", "when_to_use", "args", "rules"}
    assert entries["interface"]["rules"][0]["intent"] == "interface_check"
    assert entries["ml_classify"]["rules"] == []
    json.dumps(entries)


def test_intents_and_next_intent(registry):
    assert registry.intents() == ["bgp_state_check", "config_check", "interface_check", "tcp_port_check"]
    assert registry.next_intent_after("bgp_state") == "interface_check"
    assert registry.next_intent_after("config") is None
    assert registry.next_intent_after("ml_classify") is None


@pytest.mark.parametrize("tool_id, args, message", [
    ("show_run_everything", {}, "unknown tool"),
    ("interface", {"iface": "eth1"}, "no argument 'iface'"),
    ("interface", {"interface": 1}, "must be a string"),
    ("interface", {"interface": "eth1; reboot"}, "not allowed"),
    ("bgp_state", {"verbose": True}, "allowed: none"),
    ("interface", ["eth1"], "must be an object"),
])
def test_validate_rejects(registry, tool_id, args, message):
    with pytest.raises(ToolCallError, match=message):
        registry.validate(tool_id, args)


def test_validate_drops_context_keys_and_nones(registry):
    # host/peer always come from the run, whatever the LLM sends
    assert registry.validate("tcp_port", {"host": "10.6.6.6", "peer_ip": "10.6.6.7"}) == {}
    assert registry.validate("interface", {"interface": None}) == {}
    assert registry.validate("interface", {"interface": "eth1"}) == {"interface": "eth1"}
    assert registry.validate("bgp_state", None) == {}


def test_payload_fills_context(registry):
    assert registry.payload("bgp_state", {}, CTX) == {"host": HOST, "peer": PEER}
    assert registry.payload("tcp_port", {}, CTX) == {"host": HOST, "peer_ip": PEER}
    assert registry.payload("interface", {"interface": "eth1"}, CTX) == {"host": HOST, "interface": "eth1"}
    assert registry.payload("bgp_state", {}, {"host": HOST, "peer": None}) == {"host": HOST}


def test_execute_device_tool(lab):
    client = lab("interface_down")
    registry = ToolRegistry.load(client)
    result = registry.execute(client, "interface", {"interface": "eth1"}, CTX)
    assert result["success"] and list(result["parsed"]["interfaces"]) == ["eth1"]
    assert client.device.commands == [(HOST, "show interface eth1")]


def test_execute_turns_http_errors_into_failed_results(registry):
    class BrokenClient:
        def call_tool(self, endpoint, payload):
            raise requests.ConnectionError("API is down")
    result = registry.execute(BrokenClient(), "bgp_state", {}, CTX)
    assert result["success"] is False and "API is down" in result["error"]


def test_ml_classify_over_gathered_evidence(lab):
    client = lab("neighbor_shutdown")
    registry = ToolRegistry.load(client)
    evidence = []
    for tool_id in ("bgp_state", "config"):
        r = registry.execute(client, tool_id, {}, CTX)
        evidence.append({"tool": tool_id, **{k: r[k] for k in ("success", "error", "parsed", "raw_output")}})

    result = registry.execute(client, "ml_classify", {}, CTX, evidence)
    assert result["success"]
    assert result["parsed"]["label"] == "neighbor_shutdown"
    assert result["parsed"]["tools_classified"] == ["bgp_state", "config"]
    assert PEER in result["parsed"]["root_cause"]


def test_ml_classify_needs_device_evidence(registry):
    result = registry.execute(None, "ml_classify", {}, CTX, [])
    assert result["success"] is False and "no device tool output" in result["error"]


def test_ml_classify_has_no_rule_finding():
    assert evaluate("ml_classify", {"success": True, "parsed": {"label": "healthy"}}, CTX) == {"decision": "continue"}
