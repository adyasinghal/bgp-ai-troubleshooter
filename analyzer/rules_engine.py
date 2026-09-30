"""The reasoning loop: ask the rulebook, call the tool, decide, escalate.

Escalation order: rule chain -> ML engine -> LLM -> human.
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

# Hints are for the LLM agent; the rule chain ignores them.
STATE_HINTS = {
    "Active": ("Active: the TCP session to the peer is not forming or keeps being closed. "
               "Call bgp_neighbor next: it shows whether TCP ever came up and the last reset reason."),
    "Connect": ("Connect: the TCP session to the peer is not forming. "
                "Call bgp_neighbor next: it shows whether TCP ever came up and the last reset reason."),
    "Idle": ("Idle with no reason: the neighbor is NOT administratively shut down (that shows as Idle (Admin)). "
             "The session keeps failing and restarting, usually because the OPEN exchange is rejected "
             "(remote-as mismatch, router-id conflict) or the peer doesn't have this router configured. "
             "Call bgp_neighbor next: it shows the NOTIFICATION behind it."),
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
    """decision: root_cause | healthy | continue, plus cause/fix/fault_class when decided."""
    if not result.get("success", True):
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
        return {"decision": "continue", **({"hint": hint} if hint else {})}

    if tool_id == "bgp_neighbor":
        return _neighbor_finding(parsed, ctx)

    if tool_id == "route_table":
        return {"decision": "continue", "hint": _route_table_hint(parsed)}

    if tool_id == "route_map":
        return {"decision": "continue", "hint": _route_map_hint(parsed)}

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
        if parsed.get("has_baseline") and parsed.get("drifted"):
            return {"decision": "root_cause", "fault_class": "config_drift",
                    "cause": "Running config has drifted from the baseline",
                    "fix": "Review the diff and restore the correct BGP settings."}
        return {"decision": "continue", "hint": _neighbor_facts(result.get("raw_output") or "", ctx.get("peer"))}

    return {"decision": "continue"}


def _route_table_hint(p: dict) -> str:
    if p.get("in_table") is False:
        return (f"This router has no route for {p.get('prefix')}: the peer doesn't advertise it, or an inbound "
                f"route-map/prefix-list filters it. Check route_map: a deny entry with invoked > 0 points at filtering.")
    best = p.get("best_path")
    if not best:
        return "No best path in the table."
    others = p.get("paths_not_selected", [])
    if best.get("best_reason"):
        return (f"Best path via {best['next_hop']} wins on {best['best_reason']}; the other {len(others)} path(s) "
                f"lose at that step of the best-path order. If a route-map set the deciding attribute, check route_map.")
    return f"{p.get('path_count')} path(s). Pass a prefix to see why the best path wins."


def _route_map_hint(p: dict) -> str:
    denies = [f"{m['name']} seq {m['sequence']} ({'; '.join(m['match_clauses']) or 'matches everything'})"
              for m in p.get("route_maps", []) if m["action"] == "deny" and m.get("invoked")]
    sets = [f"{m['name']} seq {m['sequence']} sets {'; '.join(m['set_clauses'])}"
            for m in p.get("route_maps", []) if m["set_clauses"]]
    parts = []
    if denies:
        parts.append("Deny entries that matched routes: " + ", ".join(denies)
                     + ". Routes they match are filtered (route_filtered, or route_as_intended if that's the intended policy).")
    if sets:
        parts.append("Entries that change attributes: " + ", ".join(sets) + ".")
    return " ".join(parts) or "No route-map entry denies routes or changes attributes."


def _neighbor_finding(p: dict, ctx: dict) -> dict:
    peer = ctx.get("peer")
    if not p.get("configured", True):
        return {"decision": "root_cause", "fault_class": "neighbor_missing",
                "cause": f"Neighbor {peer} is not configured on this router",
                "fix": f"router bgp <local-asn> / neighbor {peer} remote-as <peer-asn>"}
    if p.get("state") == "Established":
        return {"decision": "healthy", "fault_class": "healthy",
                "cause": f"BGP peer {peer} is Established",
                "fix": "No action needed — the session is up."}

    local_as, remote_as = p.get("local_as"), p.get("remote_as")
    me = p.get("local_host") or "<this router>"
    on_peer = f"on {p.get('hostname') or peer}"
    if p.get("admin_shutdown"):
        return {"decision": "root_cause", "fault_class": "neighbor_shutdown",
                "cause": f"Neighbor {peer} is administratively shut down on this router",
                "fix": f"router bgp {local_as} / no neighbor {peer} shutdown"}

    note = p.get("notification") or {}
    error, sent = note.get("error", ""), note.get("direction") == "sent"
    if "Bad Peer AS" in error and sent:
        actual = p.get("peer_open_as")
        return {"decision": "root_cause", "fault_class": "remote_as_mismatch",
                "cause": f"This router expects AS {remote_as} for {peer}"
                         + (f", but the peer uses AS {actual}" if actual else ", but the peer's OPEN has another AS"),
                "fix": f"router bgp {local_as} / neighbor {peer} remote-as {actual or '<peer-asn>'}"}
    if "Bad Peer AS" in error:
        return {"decision": "root_cause", "fault_class": "remote_as_mismatch",
                "cause": f"{peer} rejects this router's AS {local_as}: its remote-as for {me} is wrong",
                "fix": f"{on_peer}: router bgp {remote_as} / neighbor {me} remote-as {local_as}"}
    if "Peer De-configured" in error and not sent:
        return {"decision": "root_cause", "fault_class": "neighbor_missing",
                "cause": f"{peer} removed its neighbor config for this router",
                "fix": f"{on_peer}: router bgp {remote_as} / neighbor {me} remote-as {local_as}"}
    if "Administrative Shutdown" in error and not sent:
        return {"decision": "root_cause", "fault_class": "neighbor_shutdown",
                "cause": f"{peer} has shut down its neighbor for this router",
                "fix": f"{on_peer}: router bgp {remote_as} / no neighbor {me} shutdown"}

    if p.get("connections_established") == 0:
        return {"decision": "continue",
                "hint": f"State {p.get('state')} and the TCP session has never come up (0 connections), "
                        f"so the fault is below BGP: check the interface to the peer and TCP port 179. "
                        f"'Waiting for peer OPEN' only means no OPEN ever arrived."}
    return {"decision": "continue",
            "hint": f"State {p.get('state')}; the session came up {p.get('connections_established', '?')} "
                    f"time(s) and dropped {p.get('connections_dropped', '?')}. "
                    f"Last reset: {p.get('last_reset') or 'never'}."}


def _neighbor_facts(running: str, peer: str | None) -> str:
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
                                  source="rules", fault_class=outcome["fault_class"])
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
        return Verdict(True, cause, fix, checked, evidence, source="ml", fault_class=ml.label,
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
