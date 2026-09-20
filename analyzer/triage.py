"""Triage: turn a plain-language question into a starting intent."""


def triage(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ("interface", "link", "cable")):
        return "interface_check"
    if any(w in q for w in ("tcp", "179", "reachab", "firewall")):
        return "tcp_port_check"
    if any(w in q for w in ("config", "mismatch", "remote-as", "as number")):
        return "config_check"
    return "bgp_state_check"   # default: start from BGP session state