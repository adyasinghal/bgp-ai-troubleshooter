"""Unit tests for diagnostic tools (TCP port, Config, Interface, BGP state)."""
import pytest
from unittest.mock import MagicMock

from tools.device_client import DeviceResult
from tools.tcp_port import TCPPortTool
from tools.config import ConfigTool
from tools.interface import InterfaceTool
from tools.bgp_state import BGPStateTool


# ==================================================
# TCP PORT TOOL TESTS
# ==================================================

def test_tcp_port_parse_reachable():
    tool = TCPPortTool(device_client=MagicMock())
    parsed = tool._parse("REACHABLE\n", peer_ip="172.20.20.3", port=179)
    assert parsed["reachable"] is True
    assert parsed["peer_ip"] == "172.20.20.3"
    assert parsed["port"] == 179


def test_tcp_port_parse_unreachable():
    tool = TCPPortTool(device_client=MagicMock())
    # Crucial test: "REACHABLE" substring is inside "UNREACHABLE", but should evaluate to False
    parsed = tool._parse("UNREACHABLE\n", peer_ip="172.20.20.3", port=179)
    assert parsed["reachable"] is False


def test_tcp_port_parse_unreachable_with_error_prefix():
    tool = TCPPortTool(device_client=MagicMock())
    output = "bash: connect: Connection refused\n/dev/tcp/172.20.20.3/179: Connection refused\nUNREACHABLE"
    parsed = tool._parse(output, peer_ip="172.20.20.3", port=179)
    assert parsed["reachable"] is False


def test_tcp_port_parse_unexpected_output():
    tool = TCPPortTool(device_client=MagicMock())
    parsed = tool._parse("timeout: command expired", peer_ip="172.20.20.3", port=179)
    assert parsed["reachable"] is False

    parsed_empty = tool._parse("", peer_ip="172.20.20.3", port=179)
    assert parsed_empty["reachable"] is False


def test_tcp_port_run_mocked():
    mock_client = MagicMock()
    mock_client.run_raw.return_value = DeviceResult(
        host="router1",
        command="timeout 3 ...",
        success=True,
        output="REACHABLE",
        error=None,
    )
    tool = TCPPortTool(device_client=mock_client)
    res = tool.run("router1", "172.20.20.3")
    assert res.tool_id == "tcp_port"
    assert res.success is True
    assert res.parsed["reachable"] is True


# ==================================================
# CONFIG TOOL TESTS
# ==================================================

def test_config_parse_matching_baseline():
    tool = ConfigTool(device_client=MagicMock())
    config = "router bgp 65001\n neighbor 172.20.20.3 remote-as 65002\n"
    parsed = tool._parse(output=config, baseline=config, success=True)
    assert parsed["has_baseline"] is True
    assert parsed["drifted"] is False
    assert parsed["diff"] == []


def test_config_parse_drifted_baseline():
    tool = ConfigTool(device_client=MagicMock())
    baseline = "router bgp 65001\n neighbor 172.20.20.3 remote-as 65002\n"
    running = "router bgp 65001\n neighbor 172.20.20.3 remote-as 65099\n"
    parsed = tool._parse(output=running, baseline=baseline, success=True)
    assert parsed["has_baseline"] is True
    assert parsed["drifted"] is True
    assert len(parsed["diff"]) > 0


def test_config_parse_missing_baseline():
    tool = ConfigTool(device_client=MagicMock())
    running = "router bgp 65001\n neighbor 172.20.20.3 remote-as 65002\n"
    # When baseline is None / missing, it must NOT report config drift!
    parsed = tool._parse(output=running, baseline=None, success=True)
    assert parsed["has_baseline"] is False
    assert parsed["drifted"] is False
    assert parsed["diff"] == []


def test_config_parse_command_failure():
    tool = ConfigTool(device_client=MagicMock())
    baseline = "router bgp 65001\n"
    parsed = tool._parse(output="", baseline=baseline, success=False)
    assert parsed["drifted"] is False
    assert parsed["diff"] == []


def test_config_run_with_missing_baseline(monkeypatch):
    mock_client = MagicMock()
    mock_client.run_vtysh.return_value = DeviceResult(
        host="router_nobaseline",
        command="show running-config",
        success=True,
        output="router bgp 65001\n",
    )
    tool = ConfigTool(device_client=mock_client)
    monkeypatch.setattr(tool, "_load_baseline", lambda host: None)
    res = tool.run("router_nobaseline")
    assert res.parsed["has_baseline"] is False
    assert res.parsed["drifted"] is False


# ==================================================
# INTERFACE TOOL TESTS
# ==================================================

SAMPLE_INTERFACE_OUTPUT = """
Interface eth0 is up, line protocol is up
  Link ups:       1    last: 2026/09/24 00:00:00
  Link downs:     0    last: (never)
  vrf: default
  index 2 metric 0 mtu 1500 speed 10000 Mb/s
  flags: <UP,BROADCAST,RUNNING,MULTICAST>
  Type: Ethernet
  HWaddr: 02:42:ac:14:14:02
  inet 172.20.20.2/24
  inet6 fe80::42:acff:fe14:1402/64

Interface eth1 is down, line protocol is down
  Link ups:       0    last: (never)
  Link downs:     1    last: 2026/09/24 00:00:00
  vrf: default
  index 3 metric 0 mtu 1500 speed 10000 Mb/s
  flags: <BROADCAST,MULTICAST>
  Type: Ethernet
  HWaddr: 02:42:ac:14:14:03
  inet 10.0.1.1/24

Interface eth2 is down, line protocol is down
  administratively down
  vrf: default
  index 4 metric 0 mtu 1500
  inet 192.168.1.1/24
"""


def test_interface_tool_parse():
    tool = InterfaceTool(device_client=MagicMock())
    parsed = tool._parse(SAMPLE_INTERFACE_OUTPUT)
    ifaces = parsed["interfaces"]
    assert "eth0" in ifaces
    assert ifaces["eth0"]["link_state"] == "up"
    assert ifaces["eth0"]["admin_state"] == "up"
    assert ifaces["eth0"]["ip_address"] == "172.20.20.2/24"

    assert "eth1" in ifaces
    assert ifaces["eth1"]["link_state"] == "down"
    assert ifaces["eth1"]["admin_state"] == "up"
    assert ifaces["eth1"]["ip_address"] == "10.0.1.1/24"

    assert "eth2" in ifaces
    assert ifaces["eth2"]["link_state"] == "down"
    assert ifaces["eth2"]["admin_state"] == "down"
    assert ifaces["eth2"]["ip_address"] == "192.168.1.1/24"


# ==================================================
# BGP STATE TOOL TESTS
# ==================================================

SAMPLE_BGP_SUMMARY = """
IPv4 Unicast Summary (VRF default):
BGP router identifier 172.20.20.2, local AS number 65001 vrf-id 0
BGP table version 1
RIB entries 1, using 192 bytes of memory
Peers 2, using 43 KiB of memory

Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
172.20.20.3     4      65002        12        15        0    0    0 00:05:12            2        2 Established peer
10.0.1.2        4      65003         0         0        0    0    0    never       Active        0 Stuck peer
"""


def test_bgp_state_tool_parse():
    tool = BGPStateTool(device_client=MagicMock())
    parsed = tool._parse(SAMPLE_BGP_SUMMARY, peer="172.20.20.3")
    assert parsed["peers"]["172.20.20.3"] == "Established"
    assert parsed["peers"]["10.0.1.2"] == "Active"
    assert parsed["queried_peer_state"] == "Established"

    parsed2 = tool._parse(SAMPLE_BGP_SUMMARY, peer="10.0.1.2")
    assert parsed2["queried_peer_state"] == "Active"

    parsed3 = tool._parse(SAMPLE_BGP_SUMMARY, peer="192.168.99.99")
    assert parsed3["queried_peer_state"] == "unknown"


def test_bgp_state_tool_collect():
    mock_client = MagicMock()
    mock_client.run_vtysh.side_effect = lambda host, cmd: DeviceResult(
        host=host,
        command=cmd,
        success=True,
        output=SAMPLE_BGP_SUMMARY if "summary" in cmd else (SAMPLE_BGP_NEIGHBORS_JSON if "neighbors" in cmd else SAMPLE_BGP_ROUTES_JSON),
    )
    tool = BGPStateTool(device_client=mock_client)
    collected = tool.collect("router1")
    assert "summary" in collected
    assert "neighbors" in collected
    assert "routes" in collected
    assert collected["summary"]["success"] is True
    assert collected["neighbors"]["success"] is True
    assert collected["routes"]["success"] is True
    assert "172.20.20.3" in collected["neighbors"]["parsed"]["neighbors"]
    assert "10.0.0.0/24" in collected["routes"]["parsed"]["routes"]


# ==================================================
# BGP NEIGHBOR & ROUTE JSON PARSING TESTS
# ==================================================

SAMPLE_BGP_NEIGHBORS_JSON = """
{
  "172.20.20.3": {
    "remoteRouterId": "172.20.20.3",
    "localRouterId": "172.20.20.2",
    "bgpState": "Established",
    "remoteAs": 65002,
    "localAs": 65001,
    "holdTime": 180,
    "keepAliveTime": 60,
    "bgpTimerUpString": "00:05:12",
    "lastResetDueTo": "No error",
    "lastResetCode": 0,
    "lastResetSubcode": 0,
    "messageStats": {
      "notificationsSent": 0,
      "notificationsReceived": 0
    },
    "addressFamilyInfo": {
      "ipv4Unicast": {
        "prefixReceivedCount": 2,
        "prefixAdvertisedCount": 2
      }
    }
  }
}
"""

SAMPLE_BGP_ROUTES_JSON = """
{
  "routerId": "172.20.20.2",
  "localAS": 65001,
  "routes": {
    "10.0.0.0/24": [
      {
        "valid": true,
        "bestpath": true,
        "multipath": false,
        "pathFrom": "internal",
        "prefix": "10.0.0.0/24",
        "locPrf": 100,
        "weight": 32768,
        "metric": 0,
        "peer": "0.0.0.0",
        "origin": "IGP",
        "aspath": {
          "string": "",
          "segments": [],
          "length": 0
        },
        "nexthops": [
          {
            "ip": "0.0.0.0",
            "afi": "ipv4"
          }
        ]
      }
    ],
    "192.168.1.0/24": [
      {
        "valid": true,
        "bestpath": true,
        "multipath": false,
        "pathFrom": "external",
        "prefix": "192.168.1.0/24",
        "metric": 0,
        "weight": 0,
        "peer": "172.20.20.3",
        "origin": "IGP",
        "aspath": {
          "string": "65002",
          "segments": [65002],
          "length": 1
        },
        "nexthops": [
          {
            "ip": "172.20.20.3",
            "afi": "ipv4"
          }
        ]
      }
    ]
  }
}
"""


def test_bgp_neighbors_json_valid():
    tool = BGPStateTool(device_client=MagicMock())
    parsed = tool._parse_neighbors_json(SAMPLE_BGP_NEIGHBORS_JSON)
    neighbors = parsed["neighbors"]
    assert "172.20.20.3" in neighbors

    peer_data = neighbors["172.20.20.3"]
    assert peer_data["peer"] == "172.20.20.3"
    assert peer_data["remote_router_id"] == "172.20.20.3"
    assert peer_data["local_router_id"] == "172.20.20.2"
    assert peer_data["remote_as"] == 65002
    assert peer_data["local_as"] == 65001
    assert peer_data["bgp_state"] == "Established"
    assert peer_data["hold_time"] == 180
    assert peer_data["keepalive_time"] == 60
    assert peer_data["uptime"] == "00:05:12"
    assert peer_data["last_reset_due_to"] == "No error"
    assert peer_data["last_reset_code"] == 0
    assert peer_data["last_reset_subcode"] == 0
    assert peer_data["notifications_sent"] == 0
    assert peer_data["notifications_received"] == 0
    assert peer_data["prefix_received_count"] == 2
    assert peer_data["prefix_advertised_count"] == 2


def test_bgp_neighbors_json_empty_and_malformed():
    tool = BGPStateTool(device_client=MagicMock())
    assert tool._parse_neighbors_json("{}") == {"neighbors": {}}
    assert tool._parse_neighbors_json("") == {"neighbors": {}}
    assert tool._parse_neighbors_json(None) == {"neighbors": {}}
    assert tool._parse_neighbors_json("INVALID JSON {") == {"neighbors": {}}


def test_bgp_routes_json_valid():
    tool = BGPStateTool(device_client=MagicMock())
    parsed = tool._parse_routes_json(SAMPLE_BGP_ROUTES_JSON)
    assert parsed["router_id"] == "172.20.20.2"
    assert parsed["local_as"] == 65001

    routes = parsed["routes"]
    assert "10.0.0.0/24" in routes
    assert "192.168.1.0/24" in routes

    route_192 = routes["192.168.1.0/24"][0]
    assert route_192["valid"] is True
    assert route_192["bestpath"] is True
    assert route_192["peer"] == "172.20.20.3"
    assert route_192["origin"] == "IGP"
    assert route_192["as_path"] == "65002"
    assert route_192["as_path_segments"] == [65002]
    assert route_192["next_hops"] == ["172.20.20.3"]


def test_bgp_routes_json_missing_nexthops():
    tool = BGPStateTool(device_client=MagicMock())
    raw_json = '{"routes": {"10.0.0.0/24": [{"prefix": "10.0.0.0/24", "valid": true, "nexthops": []}]}}'
    parsed = tool._parse_routes_json(raw_json)
    assert parsed["routes"]["10.0.0.0/24"][0]["next_hops"] == []

    raw_json_no_key = '{"routes": {"10.0.0.0/24": [{"prefix": "10.0.0.0/24", "valid": true}]}}'
    parsed2 = tool._parse_routes_json(raw_json_no_key)
    assert parsed2["routes"]["10.0.0.0/24"][0]["next_hops"] == []


def test_bgp_routes_json_multiple_paths():
    tool = BGPStateTool(device_client=MagicMock())
    raw_json = """
    {
      "routes": {
        "10.0.0.0/24": [
          {"prefix": "10.0.0.0/24", "bestpath": true, "peer": "172.20.20.3", "nexthops": [{"ip": "172.20.20.3"}]},
          {"prefix": "10.0.0.0/24", "bestpath": false, "peer": "172.20.20.4", "nexthops": [{"ip": "172.20.20.4"}]}
        ]
      }
    }
    """
    parsed = tool._parse_routes_json(raw_json)
    paths = parsed["routes"]["10.0.0.0/24"]
    assert len(paths) == 2
    assert paths[0]["peer"] == "172.20.20.3"
    assert paths[0]["bestpath"] is True
    assert paths[1]["peer"] == "172.20.20.4"
    assert paths[1]["bestpath"] is False


def test_bgp_routes_json_empty_and_malformed():
    tool = BGPStateTool(device_client=MagicMock())
    assert tool._parse_routes_json("{}") == {"routes": {}}
    assert tool._parse_routes_json("") == {"routes": {}}
    assert tool._parse_routes_json(None) == {"routes": {}}
    assert tool._parse_routes_json("INVALID JSON {") == {"routes": {}}


def test_bgp_summary_json_mode():
    tool = BGPStateTool(device_client=MagicMock())
    json_summary = '{"ipv4Unicast": {"peers": {"172.20.20.3": {"state": "Established"}, "10.0.1.2": {"state": "Active"}}}}'
    parsed = tool._parse_summary(json_summary, peer="172.20.20.3")
    assert parsed["peers"]["172.20.20.3"] == "Established"
    assert parsed["peers"]["10.0.1.2"] == "Active"
    assert parsed["queried_peer_state"] == "Established"


