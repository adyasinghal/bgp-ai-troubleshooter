"""Triage: turn a plain-language question into a starting intent."""
import re

CONFIG_RE = re.compile(
    r"\b(config|configuration|configs|drift|drifting|drifted|mismatch|mismatched|remote-as|as number|settings?)\b",
    re.IGNORECASE,
)
TCP_RE = re.compile(
    r"\b(tcp|179|reachab\w*|firewall|socket|connectivity)\b",
    re.IGNORECASE,
)
INTERFACE_RE = re.compile(
    r"\b(interface|interfaces|link|cable|eth\d+|operational)\b",
    re.IGNORECASE,
)


def triage(question: str) -> str:
    """Classify user question into a starting intent for the reasoning engine."""
    q = (question or "").strip()
    if not q:
        return "bgp_state_check"

    if CONFIG_RE.search(q):
        return "config_check"
    if TCP_RE.search(q):
        return "tcp_port_check"
    if INTERFACE_RE.search(q):
        return "interface_check"

    return "bgp_state_check"  # default: start from BGP session state