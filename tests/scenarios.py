"""FRR 8.5 output for each fault scenario, taken from real run logs.

router1 (172.20.20.2) is checked; its peer is router2 (172.20.20.3).
"tcp" is the port-179 check output, "baseline" a saved config, and
"unreachable" makes every SSH call fail. tests/data has `show bgp neighbors` output.
"""
from pathlib import Path

DATA = Path(__file__).parent / "data"
HOST = "172.20.20.2"
PEER = "172.20.20.3"

_SUMMARY = """IPv4 Unicast Summary (VRF default):
BGP router identifier 172.20.20.2, local AS number 65001 vrf-id 0
BGP table version 0
RIB entries 0, using 0 bytes of memory
Peers 1, using 718 KiB of memory

Neighbor        V         AS   MsgRcvd   MsgSent   TblVer  InQ OutQ  Up/Down State/PfxRcd   PfxSnt Desc
{peer_line}

Total number of neighbors 1"""


def _summary(remote_as: int, up_down: str, state: str, pfx_snt: str) -> str:
    line = (f"172.20.20.3     4      {remote_as}        10        11        0    0    0 "
            f"{up_down} {state:>12} {pfx_snt:>8} N/A")
    return _SUMMARY.format(peer_line=line)


def _interface(name: str, index: int, up: bool, inet: str | None, loopback: bool = False) -> str:
    state = "up" if up else "down"
    if loopback:
        flags = "UP,LOOPBACK,RUNNING"
    else:
        flags = "UP,BROADCAST,RUNNING,MULTICAST" if up else "BROADCAST,MULTICAST"
    lines = [
        f"Interface {name} is {state}, line protocol is {state}",
        "  Link ups:       0    last: (never)",
        "  Link downs:     0    last: (never)",
        "  vrf: default",
        f"  index {index} metric 0 mtu 1500 speed 10000 ",
        f"  flags: <{flags}>",
        "  Type: " + ("Loopback" if loopback else "Ethernet"),
    ]
    if inet:
        lines.append(f"  inet {inet}")
    lines += ["  Interface Type " + ("Other" if loopback else "VETH"),
              "  Interface Slave Type None",
              "  protodown: off"]
    return "\n".join(lines)


def _interfaces(eth1_up: bool = True) -> str:
    return "\n".join([
        _interface("eth0", 2, True, "172.20.20.2/24"),
        _interface("eth1", 19, eth1_up, None),
        _interface("lo", 1, True, None, loopback=True),
    ])


def _running_config(neighbor_lines: list[str]) -> str:
    return "\n".join([
        "Building configuration...",
        "",
        "Current configuration:",
        "!",
        "frr version 8.5.2_git",
        "frr defaults traditional",
        "hostname router1",
        "no ipv6 forwarding",
        "service integrated-vtysh-config",
        "!",
        "router bgp 65001",
        *[f" {line}" for line in neighbor_lines],
        "exit",
        "!",
        "end",
    ])


_GOOD_CONFIG = _running_config(["neighbor 172.20.20.3 remote-as 65002"])
NEIGHBOR = "show bgp neighbors 172.20.20.3"


def _neighbor(name: str) -> str:
    return (DATA / f"neighbor_{name}.txt").read_text()


SCENARIOS = {
    # Session up; FRR prints "(Policy)" for an eBGP peer with no policy configured.
    "healthy": {
        "show bgp summary": _summary(65002, "00:01:00", "(Policy)", "(Policy)"),
        "show interface": _interfaces(),
        "tcp": "REACHABLE",
        "show running-config": _GOOD_CONFIG,
        NEIGHBOR: _neighbor("healthy"),
    },
    # GuideToRun Step 9: `neighbor 172.20.20.3 shutdown`.
    "neighbor_shutdown": {
        "show bgp summary": _summary(65002, "00:00:13", "Idle (Admin)", "0"),
        "show interface": _interfaces(),
        "tcp": "REACHABLE",
        "show running-config": _running_config(["neighbor 172.20.20.3 remote-as 65002",
                                                "neighbor 172.20.20.3 shutdown"]),
        NEIGHBOR: _neighbor("shutdown"),
    },
    # GuideToRun Step 10: remote-as set to 65009 instead of 65002, no baseline.
    "remote_as_mismatch": {
        "show bgp summary": _summary(65009, "00:00:07", "Idle", "0"),
        "show interface": _interfaces(),
        "tcp": "REACHABLE",
        "show running-config": _running_config(["neighbor 172.20.20.3 remote-as 65009"]),
        NEIGHBOR: _neighbor("remote_as"),
    },
    # Same fault as above, but a known-good baseline was saved (GuideToRun Step 11).
    "config_drift": {
        "show bgp summary": _summary(65009, "00:00:07", "Idle", "0"),
        "show interface": _interfaces(),
        "tcp": "REACHABLE",
        "show running-config": _running_config(["neighbor 172.20.20.3 remote-as 65009"]),
        "baseline": _GOOD_CONFIG,
        NEIGHBOR: _neighbor("remote_as"),
    },
    # router2 removed its neighbor for router1; router1 is still configured.
    "peer_deconfigured": {
        "show bgp summary": _summary(65002, "00:00:15", "Active", "0"),
        "show interface": _interfaces(),
        "tcp": "REACHABLE",
        "show running-config": _GOOD_CONFIG,
        NEIGHBOR: _neighbor("peer_deconfigured"),
    },
    "interface_down": {
        "show bgp summary": _summary(65002, "00:00:30", "Active", "0"),
        "show interface": _interfaces(eth1_up=False),
        "tcp": "UNREACHABLE",
        "show running-config": _GOOD_CONFIG,
        NEIGHBOR: _neighbor("never_up"),
    },
    "tcp_blocked": {
        "show bgp summary": _summary(65002, "00:00:30", "Connect", "0"),
        "show interface": _interfaces(),
        "tcp": "UNREACHABLE",
        "show running-config": _GOOD_CONFIG,
        NEIGHBOR: _neighbor("never_up"),
    },
    # The router's SSH is down, so every tool fails.
    "device_unreachable": {"unreachable": True},
}
