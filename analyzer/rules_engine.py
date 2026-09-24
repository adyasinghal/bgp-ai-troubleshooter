"""The reasoning loop: ask the rulebook, call the tool, decide, escalate.

Escalation order: rule chain -> ML engine -> LLM -> human.
"""
from analyzer import llm_escalation, ml_engine
from analyzer.triage import triage
from analyzer.verdict import Verdict

ML_CONFIDENCE_THRESHOLD = 0.7   # below this, the ML guess goes to the LLM as a hint


def _payload(tool_id: str, ctx: dict) -> dict:
    host, peer = ctx["host"], ctx.get("peer")
    if tool_id == "bgp_state":
        return {"host": host, "peer": peer}
    if tool_id == "tcp_port":
        return {"host": host, "peer_ip": peer, "port": 179}
    return {"host": host}   # interface + config just need the host


def _evaluate(tool_id: str, result: dict, ctx: dict) -> dict:
    """decision: root_cause | healthy | continue."""
    if not result.get("success", True):
        return {"decision": "continue"}   # tool failed to run; its output proves nothing

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
        # With no baseline the whole running config shows up as "drift", which proves nothing.
        if parsed.get("has_baseline") and parsed.get("drifted"):
            return {"decision": "root_cause",
                    "cause": "Running config has drifted from the baseline",
                    "fix": "Review the diff and restore the correct BGP settings."}
        return {"decision": "continue"}

    return {"decision": "continue"}


def diagnose(client, question: str, host: str, peer: str,
             use_ml: bool = True, use_llm: bool = True) -> Verdict:
    intent = triage(question)
    ctx = {"host": host, "peer": peer}
    checked, evidence = [], []

    while intent:
        rule = client.get_rule(intent)
        for tool in rule["tools"]:
            tool_id = tool["tool_id"]
            result = client.call_tool(tool["endpoint"], _payload(tool_id, ctx))
            checked.append(tool_id)
            evidence.append({
                "tool": tool_id,
                "success": result.get("success"),
                "error": result.get("error"),
                "parsed": result.get("parsed"),
                "raw_output": result.get("raw_output"),
            })

            outcome = _evaluate(tool_id, result, ctx)
            if outcome["decision"] in ("root_cause", "healthy"):
                verdict = Verdict(True, outcome["cause"], outcome["fix"], checked, evidence,
                                  source="rules")
                if use_ml:   # second opinion, shown alongside the rule's answer
                    verdict.ml_prediction = ml_engine.predict(evidence, peer).to_dict()
                return verdict
        intent = rule.get("next_intent_on_fail")   # escalate

    # Rule chain exhausted -> ML engine
    ml = ml_engine.predict(evidence, peer) if use_ml else None
    if ml and ml.label != "unknown" and ml.confidence >= ML_CONFIDENCE_THRESHOLD:
        cause, fix = ml.describe(peer)
        return Verdict(True, cause, fix, checked, evidence, source="ml",
                       confidence=f"{ml.confidence:.2f}", ml_prediction=ml.to_dict())

    # ML unsure -> LLM
    verdict = Verdict(False, "No root cause found by the rule chain.",
                      "Escalate to a human.", checked, evidence,
                      ml_prediction=ml.to_dict() if ml else None)
    if not use_llm:
        return verdict
    try:
        llm = llm_escalation.escalate(question, host, peer, checked, evidence,
                                      verdict.ml_prediction)
    except llm_escalation.LLMUnavailable as e:
        verdict.notes.append(f"LLM escalation skipped: {e}")
        return verdict

    return Verdict(llm["resolved"], llm["root_cause"], llm["suggested_fix"], checked, evidence,
                   source="llm", confidence=llm["confidence"],
                   ml_prediction=verdict.ml_prediction, next_checks=llm["next_checks"],
                   notes=[f"Diagnosed by {llm['model']}"])
