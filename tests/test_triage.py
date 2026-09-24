"""Unit tests for deterministic triage logic."""
import pytest
from analyzer.triage import triage


@pytest.mark.parametrize(
    "question,expected_intent",
    [
        # BGP state starting point (default & session keywords)
        ("Why is BGP down?", "bgp_state_check"),
        ("Why is BGP down on Router1?", "bgp_state_check"),
        ("BGP session is not coming up", "bgp_state_check"),
        ("Why is my neighbor stuck in Active?", "bgp_state_check"),
        ("Neighbor stuck in Connect", "bgp_state_check"),
        ("Why is the BGP session in Idle state?", "bgp_state_check"),
        ("What is the state of peer 172.20.20.3?", "bgp_state_check"),
        ("", "bgp_state_check"),

        # Interface starting point
        ("Why is interface eth1 down?", "interface_check"),
        ("Why is the link down?", "interface_check"),
        ("Is the interface operational?", "interface_check"),
        ("Check eth0 link state", "interface_check"),
        ("Check if the cable is disconnected", "interface_check"),
        ("Why is eth2 down?", "interface_check"),

        # TCP port starting point
        ("Can router1 reach router2 on port 179?", "tcp_port_check"),
        ("Is BGP port 179 reachable?", "tcp_port_check"),
        ("Check TCP connectivity to the peer", "tcp_port_check"),
        ("Check reachability to peer 10.0.1.2", "tcp_port_check"),
        ("Is there a firewall blocking port 179?", "tcp_port_check"),
        ("TCP socket connection failure", "tcp_port_check"),

        # Config starting point
        ("Did the configuration change?", "config_check"),
        ("Check for configuration drift", "config_check"),
        ("Why did the BGP configuration change?", "config_check"),
        ("Is there a config mismatch?", "config_check"),
        ("Remote-as mismatch between peers", "config_check"),
        ("AS number configuration error", "config_check"),
        ("Did someone change the bgp settings?", "config_check"),
    ],
)
def test_triage_classification(question, expected_intent):
    assert triage(question) == expected_intent


def test_triage_case_insensitivity():
    assert triage("WHY IS INTERFACE ETH1 DOWN?") == "interface_check"
    assert triage("check tcp connectivity") == "tcp_port_check"
    assert triage("DID CONFIGURATION CHANGE?") == "config_check"
    assert triage("WHY IS BGP DOWN?") == "bgp_state_check"
