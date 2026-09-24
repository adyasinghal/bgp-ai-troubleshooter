"""Unit tests for FastAPI REST API endpoints."""
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

from api.main import app, bgp_tool, interface_tool, tcp_tool, config_tool
from tools.base_tool import ToolResult

client = TestClient(app)


def test_api_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_api_rules_intents():
    response = client.get("/rules/intents")
    assert response.status_code == 200
    intents = response.json()["intents"]
    assert "bgp_state_check" in intents
    assert "interface_check" in intents
    assert "tcp_port_check" in intents
    assert "config_check" in intents


def test_api_rules_by_intent():
    response = client.get("/rules/bgp_state_check")
    assert response.status_code == 200
    data = response.json()
    assert data["intent"] == "bgp_state_check"
    assert data["next_intent_on_fail"] == "interface_check"
    assert len(data["tools"]) == 1
    assert data["tools"][0]["tool_id"] == "bgp_state"


def test_api_rules_nonexistent_intent():
    response = client.get("/rules/nonexistent_intent")
    assert response.status_code == 404


def test_api_tools_list():
    response = client.get("/rules/tools")
    assert response.status_code == 200
    tool_ids = [t["tool_id"] for t in response.json()["tools"]]
    assert "bgp_state" in tool_ids
    assert "interface" in tool_ids
    assert "tcp_port" in tool_ids
    assert "config" in tool_ids


def test_api_tool_bgp_endpoint(monkeypatch):
    mock_res = ToolResult(
        tool_id="bgp_state",
        host="router1",
        command="show bgp summary",
        success=True,
        raw_output="...",
        parsed={"queried_peer_state": "Established", "peers": {"172.20.20.3": "Established"}},
    )
    monkeypatch.setattr(bgp_tool, "run", lambda host, peer: mock_res)

    response = client.post("/tools/bgp/state", json={"host": "router1", "peer": "172.20.20.3"})
    assert response.status_code == 200
    data = response.json()
    assert data["tool_id"] == "bgp_state"
    assert data["parsed"]["queried_peer_state"] == "Established"


def test_api_tool_interface_endpoint(monkeypatch):
    mock_res = ToolResult(
        tool_id="interface",
        host="router1",
        command="show interface detail",
        success=True,
        raw_output="...",
        parsed={"interfaces": {"eth1": {"link_state": "up", "admin_state": "up"}}},
    )
    monkeypatch.setattr(interface_tool, "run", lambda host, interface: mock_res)

    response = client.post("/tools/interface/detail", json={"host": "router1"})
    assert response.status_code == 200
    data = response.json()
    assert data["tool_id"] == "interface"
    assert "eth1" in data["parsed"]["interfaces"]


def test_api_tool_tcp_endpoint(monkeypatch):
    mock_res = ToolResult(
        tool_id="tcp_port",
        host="router1",
        command="timeout 3 ...",
        success=True,
        raw_output="REACHABLE",
        parsed={"peer_ip": "172.20.20.3", "port": 179, "reachable": True},
    )
    monkeypatch.setattr(tcp_tool, "run", lambda host, peer_ip, port: mock_res)

    response = client.post("/tools/tcp/check", json={"host": "router1", "peer_ip": "172.20.20.3", "port": 179})
    assert response.status_code == 200
    data = response.json()
    assert data["tool_id"] == "tcp_port"
    assert data["parsed"]["reachable"] is True


def test_api_tool_config_endpoint(monkeypatch):
    mock_res = ToolResult(
        tool_id="config",
        host="router1",
        command="show running-config",
        success=True,
        raw_output="...",
        parsed={"has_baseline": True, "diff": [], "drifted": False},
    )
    monkeypatch.setattr(config_tool, "run", lambda host: mock_res)

    response = client.post("/tools/config/diff", json={"host": "router1"})
    assert response.status_code == 200
    data = response.json()
    assert data["tool_id"] == "config"
    assert data["parsed"]["drifted"] is False
