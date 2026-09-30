"""The four tools' parsers, fed real FRR 8.5 output through FakeDeviceClient."""
import pytest

from tests.fakes import FakeDeviceClient
from tests.scenarios import HOST, PEER, SCENARIOS
from tools.bgp_state import BGPStateTool
from tools.config import ConfigTool
from tools.interface import InterfaceTool
from tools.tcp_port import TCPPortTool


def run(tool_cls, scenario: str, *args):
    return tool_cls(FakeDeviceClient(SCENARIOS[scenario])).run(HOST, *args)


@pytest.mark.parametrize("scenario, state", [
    ("healthy", "Established"),          # "(Policy)" in the State/PfxRcd column
    ("neighbor_shutdown", "Idle"),       # "Idle (Admin)"
    ("remote_as_mismatch", "Idle"),
    ("interface_down", "Active"),
    ("tcp_blocked", "Connect"),
])
def test_bgp_state_reads_peer_state(scenario, state):
    result = run(BGPStateTool, scenario, PEER)
    assert result.success
    assert result.parsed == {"peers": {PEER: state}, "queried_peer_state": state}


def test_bgp_state_unknown_peer():
    assert run(BGPStateTool, "healthy", "10.9.9.9").parsed["queried_peer_state"] == "unknown"


def test_interface_all_up():
    ifaces = run(InterfaceTool, "healthy").parsed["interfaces"]
    assert set(ifaces) == {"eth0", "eth1", "lo"}
    assert all(i["link_state"] == "up" and i["admin_state"] == "up" for i in ifaces.values())
    assert ifaces["eth0"]["ip_address"] == "172.20.20.2/24"


def test_interface_down_is_link_and_admin_down():
    eth1 = run(InterfaceTool, "interface_down").parsed["interfaces"]["eth1"]
    assert eth1 == {"link_state": "down", "admin_state": "down", "ip_address": None}


def test_interface_by_name():
    result = run(InterfaceTool, "interface_down", "eth1")
    assert result.command == "show interface eth1"
    assert list(result.parsed["interfaces"]) == ["eth1"]


@pytest.mark.parametrize("scenario, reachable", [("healthy", True), ("tcp_blocked", False)])
def test_tcp_port(scenario, reachable):
    result = run(TCPPortTool, scenario, PEER)
    assert result.parsed == {"peer_ip": PEER, "port": 179, "reachable": reachable}


def test_config_without_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.config.BASELINE_DIR", tmp_path)
    parsed = run(ConfigTool, "remote_as_mismatch").parsed
    # With no baseline the whole running config counts as drift; the rules ignore it.
    assert parsed["has_baseline"] is False
    assert parsed["drifted"] is True


def test_config_diff_against_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr("tools.config.BASELINE_DIR", tmp_path)
    ConfigTool().save_baseline(HOST, SCENARIOS["config_drift"]["baseline"])
    parsed = run(ConfigTool, "config_drift").parsed
    assert parsed["has_baseline"] and parsed["drifted"]
    assert "- neighbor 172.20.20.3 remote-as 65002" in parsed["diff"]
    assert "+ neighbor 172.20.20.3 remote-as 65009" in parsed["diff"]


def test_ssh_failure_is_reported_not_raised():
    result = run(BGPStateTool, "device_unreachable", PEER)
    assert result.success is False
    assert "Unable to connect" in result.error
