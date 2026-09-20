"""The reasoning loop: ask the rulebook, call the tool, decide, escalate."""
from analyzer.triage import triage
from analyzer.verdict import Verdict


def _payload(tool_id: str, ctx: dict) -> dict:
    host, peer = ctx["host"], ctx.get("peer")
    if tool_id == "bgp_state":
        return {"host": host, "peer": peer}
    if tool_id == "tcp_port":
        return {"host": host, "peer_ip": peer, "port": 179}
    return {"host": host}   # interface + config just need the host


def _evaluate(tool_id: str, result: dict, ctx: dict) -> dict:
    """decision: root_cause | healthy | continue."""
    parsed = result.get("parsed", {})

    if tool_id == "bgp_state":
        if parsed.get("queried_peer_state") == "Established":
            return {"decision": "healthy",
                    "cause": f"BGP peer {ctx.get('peer')} is Established",
                    "fix": "No action needed — the session is up."}
        return {"decision": "continue"}   # Active/Idle/Connect -> dig deeper

    if tool_id == "interface":
        down = [n for n, i in parsed.get("interfaces", {}).items()
                if i.get("link_state") == "down" or i.get("admin_state") == "down"]
        if down:
            return {"decision": "root_cause",
                    "cause": f"Interface(s) down: {', '.join(down)}",
                    "fix": f"Bring it up: conf t / interface {down[0]} / no shutdown"}
        return {"decision": "continue"}

    if tool_id == "tcp_port":
        if parsed.get("reachable") is False:
            return {"decision": "root_cause",
                    "cause": "TCP port 179 to the peer is not reachable",
                    "fix": "Check routing/ACLs so the peer is reachable on port 179."}
        return {"decision": "continue"}

    if tool_id == "config":
        if parsed.get("drifted"):
            return {"decision": "root_cause",
                    "cause": "Running config has drifted from the baseline",
                    "fix": "Review the diff and restore the correct BGP settings."}
        return {"decision": "continue"}

    return {"decision": "continue"}


def diagnose(client, question: str, host: str, peer: str) -> Verdict:
    intent = triage(question)
    ctx = {"host": host, "peer": peer}
    checked, evidence = [], []

    while intent:
        rule = client.get_rule(intent)
        for tool in rule["tools"]:
            tool_id = tool["tool_id"]
            result = client.call_tool(tool["endpoint"], _payload(tool_id, ctx))
            checked.append(tool_id)
            evidence.append({"tool": tool_id, "parsed": result.get("parsed")})

            outcome = _evaluate(tool_id, result, ctx)
            if outcome["decision"] in ("root_cause", "healthy"):
                return Verdict(True, outcome["cause"], outcome["fix"], checked, evidence)
        intent = rule.get("next_intent_on_fail")   # escalate

    return Verdict(False, "No root cause found by the rule chain.",
                   "Escalate to a human or LLM.", checked, evidence)