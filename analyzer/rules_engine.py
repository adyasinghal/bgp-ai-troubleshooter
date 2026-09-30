"""The reasoning loop: ask the rulebook, call the tool, decide, escalate.

Escalation order: rule chain -> ML engine -> LLM -> human.

evaluate() is the rule book's verdict on a single tool result. The rule chain
below acts on it directly; the LLM agent gets it as a deterministic finding
next to each tool's output.
"""
import json
import logging

from analyzer import llm_escalation, ml_engine
from analyzer.tool_registry import ToolRegistry
from analyzer.triage import triage
from analyzer.verdict import Verdict

ML_CONFIDENCE_THRESHOLD = 0.7   # below this, the ML guess goes to the LLM as a hint

log = logging.getLogger(__name__)


CONNECT_ERRORS = ("NoValidConnectionsError", "Unable to connect", "timed out", "AuthenticationException")

# What each stuck state means, for the LLM agent. The rule chain ignores hints.
STATE_HINTS = {
    "Active": "Active: the TCP session to the peer is not forming. Check the interface to the peer and TCP port 179.",
    "Connect": "Connect: the TCP session to the peer is not forming. Check the interface to the peer and TCP port 179.",
    "Idle": ("Idle with no reason: the neighbor is NOT administratively shut down (that shows as Idle (Admin)). "
             "The session keeps failing and restarting, usually because the OPEN exchange is rejected "
             "(remote-as mismatch, router-id conflict) or the peer doesn't have this router configured. "
             "Check the config's neighbor lines."),
    "OpenSent": "OpenSent: TCP is up but the OPEN negotiation fails (remote-as, router-id, capabilities, MD5).",
    "OpenConfirm": "OpenConfirm: TCP is up but the OPEN negotiation fails (remote-as, router-id, capabilities, MD5).",
    "unknown": ("The peer is not listed in `show bgp summary`, so it is most likely not configured on this router. "
                "Check the config for a `neighbor <peer> remote-as` line."),
}
REASON_HINTS = {
    "Admin": ("Idle (Admin): the neighbor is administratively shut down on THIS router "
              "(`neighbor {peer} shutdown`). FRR fix on this router: "
              "`router bgp <local-asn>` then `no neighbor {peer} shutdown`."),
    "PfxCt": "Idle (PfxCt): the peer sent more prefixes than the configured maximum-prefix allows.",
}


def evaluate(tool_id: str, result: dict, ctx: dict) -> dict:
    """decision: root_cause | healthy | continue. A decided finding also has cause,
    fix and fault_class (an ml_engine.FAULTS label, for comparing with the LLM and ML).
    Any finding may carry a hint: what the result means, for the LLM agent."""
    if not result.get("success", True):
        # tool failed to run; its output proves nothing
        error = result.get("error") or ""
        if any(e in error for e in CONNECT_ERRORS):
            return {"decision": "continue",
                    "hint": "SSH to the router failed. Every device tool will fail the same way, so "
                            "the fault is reaching the router itself (fault_class device_unreachable)."}
        return {"decision": "continue"}

    parsed = result.get("parsed", {})

    if tool_id == "bgp_state":
        state = parsed.get("queried_peer_state")
        if state == "Established":
            return {"decision": "healthy", "fault_class": "healthy",
                    "cause": f"BGP peer {ctx.get('peer')} is Established",
                    "fix": "No action needed — the session is up."}
        hint = REASON_HINTS.get(parsed.get("queried_peer_state_reason")) or STATE_HINTS.get(state)
        if hint:
            hint = hint.format(peer=ctx.get("peer") or "<peer>")
        return {"decision": "continue", **({"hint": hint} if hint else {})}   # dig deeper

    if tool_id == "interface":
        down = [n for n, i in parsed.get("interfaces", {}).items()
                if i.get("link_state") == "down" or i.get("admin_state") == "down"]
        if down:
            return {"decision": "root_cause", "fault_class": "interface_down",
                    "cause": f"Interface(s) down: {', '.join(down)}",
                    "fix": f"Bring it up: conf t / interface {down[0]} / no shutdown"}
        return {"decision": "continue"}

    if tool_id == "tcp_port":
        if parsed.get("reachable") is False:
            return {"decision": "root_cause", "fault_class": "tcp_unreachable",
                    "cause": "TCP port 179 to the peer is not reachable",
                    "fix": "Check routing/ACLs so the peer is reachable on port 179."}
        return {"decision": "continue"}

    if tool_id == "config":
        # With no baseline the whole running config shows up as "drift", which proves nothing.
        if parsed.get("has_baseline") and parsed.get("drifted"):
            return {"decision": "root_cause", "fault_class": "config_drift",
                    "cause": "Running config has drifted from the baseline",
                    "fix": "Review the diff and restore the correct BGP settings."}
        return {"decision": "continue", "hint": _neighbor_facts(result.get("raw_output") or "", ctx.get("peer"))}

    return {"decision": "continue"}


def _neighbor_facts(running: str, peer: str | None) -> str:
    """What the running config says about the peer, in one sentence."""
    if not peer:
        return "No peer given, so the config's neighbor lines weren't checked."
    lines = [l.strip() for l in running.splitlines() if l.strip().startswith(f"neighbor {peer} ")]
    local_as = next((l.split()[2] for l in running.splitlines()
                     if l.startswith("router bgp ") and len(l.split()) > 2), None)
    if not lines:
        return (f"The running config has no `neighbor {peer}` lines: the neighbor is not configured "
                f"on this router (fault_class neighbor_missing).")
    remote_as = next((l.split()[3] for l in lines if " remote-as " in f" {l} " and len(l.split()) > 3), None)
    shut = any(l == f"neighbor {peer} shutdown" for l in lines)
    facts = [f"This router (local AS {local_as or '?'}) configures neighbor {peer}"
             + (f" with remote-as {remote_as}" if remote_as else " without a remote-as")]
    facts.append("and it IS shut down (`neighbor ... shutdown`)" if shut
                 else "and it is NOT shut down (no `shutdown` line)")
    hint = " ".join(facts) + "."
    if shut:
        hint += (f" FRR fix on this router: `router bgp {local_as or '<local-asn>'}` then "
                 f"`no neighbor {peer} shutdown`.")
    elif remote_as:
        hint += (f" If the session still fails, compare remote-as {remote_as} with the AS the peer "
                 f"actually runs (`show running-config` on {peer}, line `router bgp <asn>`): a difference "
                 f"is a remote-as mismatch. FRR fix on THIS router: `router bgp {local_as or '<local-asn>'}` "
                 f"then `neighbor {peer} remote-as <the peer's real AS>`.")
    return hint


def diagnose(client, question: str, host: str, peer: str,
             use_ml: bool = True, use_llm: bool = True) -> Verdict:
    intent = triage(question)
    log.info("Triage: starting intent %s", intent)
    ctx = {"host": host, "peer": peer}
    registry = ToolRegistry.load(client)
    checked, evidence = [], []

    while intent:
        rule = client.get_rule(intent)
        log.info("Rule %s: tools %s, next intent on fail %s", intent,
                 [t["tool_id"] for t in rule["tools"]], rule.get("next_intent_on_fail"))
        for tool in rule["tools"]:
            tool_id = tool["tool_id"]
            result = registry.execute(client, tool_id, {}, ctx, evidence)
            if result.get("success", True):
                log.info("Tool %s succeeded", tool_id)
            else:
                log.warning("Tool %s failed: %s", tool_id, result.get("error"))
            log.debug("Tool %s parsed output: %s", tool_id,
                      json.dumps(result.get("parsed"), sort_keys=True, default=str))
            log.debug("Tool %s raw output:\n%s", tool_id, result.get("raw_output"))
            checked.append(tool_id)
            evidence.append({
                "tool": tool_id,
                "success": result.get("success"),
                "error": result.get("error"),
                "parsed": result.get("parsed"),
                "raw_output": result.get("raw_output"),
            })

            outcome = evaluate(tool_id, result, ctx)
            log.info("Rule decision after %s: %s%s", tool_id, outcome["decision"],
                     f" ({outcome['cause']})" if "cause" in outcome else "")
            if outcome["decision"] in ("root_cause", "healthy"):
                verdict = Verdict(True, outcome["cause"], outcome["fix"], checked, evidence,
                                  source="rules")
                if use_ml:   # second opinion, shown alongside the rule's answer
                    ml = ml_engine.predict(evidence, peer)
                    log.info("ML second opinion: %s (%.2f)", ml.label, ml.confidence)
                    verdict.ml_prediction = ml.to_dict()
                return verdict
        intent = rule.get("next_intent_on_fail")   # escalate
        if intent:
            log.info("Rule unresolved; escalating to intent %s", intent)

    # Rule chain exhausted -> ML engine
    log.info("Rule chain exhausted without a root cause")
    ml = ml_engine.predict(evidence, peer) if use_ml else None
    if not ml:
        log.info("ML engine skipped (--no-ml)")
    elif ml.label != "unknown" and ml.confidence >= ML_CONFIDENCE_THRESHOLD:
        log.info("ML engine decided: %s (%.2f >= threshold %.2f)",
                 ml.label, ml.confidence, ML_CONFIDENCE_THRESHOLD)
        cause, fix = ml.describe(peer)
        return Verdict(True, cause, fix, checked, evidence, source="ml",
                       confidence=f"{ml.confidence:.2f}", ml_prediction=ml.to_dict())
    else:
        log.info("ML engine unsure: %s (%.2f, threshold %.2f)",
                 ml.label, ml.confidence, ML_CONFIDENCE_THRESHOLD)

    # ML unsure -> LLM
    verdict = Verdict(False, "No root cause found by the rule chain.",
                      "Escalate to a human.", checked, evidence,
                      ml_prediction=ml.to_dict() if ml else None)
    if not use_llm:
        log.info("LLM escalation skipped (--no-llm)")
        return verdict
    try:
        llm = llm_escalation.escalate(question, host, peer, checked, evidence,
                                      verdict.ml_prediction)
    except llm_escalation.LLMUnavailable as e:
        log.warning("LLM escalation skipped: %s", e)
        verdict.notes.append(f"LLM escalation skipped: {e}")
        return verdict
    log.info("LLM decided: resolved=%s confidence=%s root cause: %s",
             llm["resolved"], llm["confidence"], llm["root_cause"])

    return Verdict(llm["resolved"], llm["root_cause"], llm["suggested_fix"], checked, evidence,
                   source="llm", confidence=llm["confidence"],
                   ml_prediction=verdict.ml_prediction, next_checks=llm["next_checks"],
                   notes=[f"Diagnosed by {llm['model']}"])
