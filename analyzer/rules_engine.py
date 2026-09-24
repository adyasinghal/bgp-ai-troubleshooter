"""The reasoning loop: ask the rulebook, call the tool, decide, escalate."""
import ipaddress
import os
from typing import Optional

from analyzer.llm_engine import EvidenceBuilder, LLMEngine
from analyzer.triage import triage
from analyzer.verdict import Verdict


def _payload(tool_id: str, ctx: dict) -> dict:
    host, peer = ctx["host"], ctx.get("peer")
    if tool_id == "bgp_state":
        return {"host": host, "peer": peer}
    if tool_id == "tcp_port":
        return {"host": host, "peer_ip": peer, "port": 179}
    return {"host": host}   # interface + config just need the host


def _is_peer_on_interface(peer_ip_str: str | None, iface_ip_str: str | None) -> bool:
    """Check if the peer IP belongs to the interface's IP subnet."""
    if not peer_ip_str or not iface_ip_str:
        return False
    try:
        peer_ip = ipaddress.ip_address(peer_ip_str.split("/")[0].strip())
        iface_net = ipaddress.ip_network(iface_ip_str.strip(), strict=False)
        return peer_ip in iface_net
    except ValueError:
        return False


def _evaluate(tool_id: str, result: dict, ctx: dict) -> dict:
    """decision: root_cause | healthy | continue."""
    if not isinstance(result, dict):
        return {"decision": "continue"}

    parsed = result.get("parsed") or {}
    if not isinstance(parsed, dict):
        return {"decision": "continue"}

    if tool_id == "bgp_state":
        if parsed.get("queried_peer_state") == "Established":
            return {"decision": "healthy",
                    "cause": f"BGP peer {ctx.get('peer')} is Established",
                    "fix": "No action needed — the session is up."}
        return {"decision": "continue"}   # Active/Idle/Connect -> dig deeper

    if tool_id == "interface":
        interfaces = parsed.get("interfaces", {})
        if not interfaces or not isinstance(interfaces, dict):
            return {"decision": "continue"}

        peer = ctx.get("peer")
        if peer:
            # Safer interface diagnosis: identify interface relevant to the peer subnet
            relevant_ifaces = [
                (name, info)
                for name, info in interfaces.items()
                if _is_peer_on_interface(peer, info.get("ip_address"))
            ]

            if relevant_ifaces:
                down_relevant = [
                    name
                    for name, info in relevant_ifaces
                    if info.get("link_state") == "down" or info.get("admin_state") == "down"
                ]
                if down_relevant:
                    target = down_relevant[0]
                    target_info = interfaces.get(target, {})
                    admin_down = target_info.get("admin_state") == "down"
                    cause_desc = "administratively down" if admin_down else "down"
                    return {
                        "decision": "root_cause",
                        "cause": f"Relevant interface {target} for peer {peer} is {cause_desc}",
                        "fix": f"Bring it up: conf t / interface {target} / no shutdown",
                    }
                # Relevant interface is up -> do not blame unrelated down interfaces
                return {"decision": "continue"}

            # Insufficient info or peer not in subnet -> safely continue
            return {"decision": "continue"}

        # If no peer is specified, check down interfaces directly
        down = [n for n, i in interfaces.items()
                if i.get("link_state") == "down" or i.get("admin_state") == "down"]
        if down:
            return {"decision": "root_cause",
                    "cause": f"Interface(s) down: {', '.join(down)}",
                    "fix": f"Bring it up: conf t / interface {down[0]} / no shutdown"}
        return {"decision": "continue"}

    if tool_id == "tcp_port":
        if parsed.get("reachable") is False:
            peer_ip = parsed.get("peer_ip") or ctx.get("peer") or "the peer"
            port = parsed.get("port", 179)
            return {"decision": "root_cause",
                    "cause": f"TCP port {port} to {peer_ip} is not reachable",
                    "fix": f"Check routing/ACLs so {peer_ip} is reachable on port {port}."}
        return {"decision": "continue"}

    if tool_id == "config":
        if parsed.get("has_baseline") and parsed.get("drifted"):
            return {"decision": "root_cause",
                    "cause": "Running config has drifted from the baseline",
                    "fix": "Review the diff and restore the correct BGP settings."}
        return {"decision": "continue"}

    return {"decision": "continue"}


def diagnose(
    client,
    question: str,
    host: str,
    peer: str,
    llm_engine: Optional[LLMEngine] = None,
    enable_llm: bool = False,
    telemetry: Optional[dict] = None,
) -> Verdict:
    intent = triage(question)
    starting_intent = intent
    ctx = {"host": host, "peer": peer}
    checked, evidence = [], []
    verdict = None

    while intent:
        rule = client.get_rule(intent)
        if not rule or not isinstance(rule, dict):
            break
        tools = rule.get("tools", [])
        for tool in tools:
            tool_id = tool.get("tool_id", "")
            if not tool_id:
                continue
            endpoint = tool.get("endpoint", "")
            try:
                result = client.call_tool(endpoint, _payload(tool_id, ctx))
            except Exception as e:
                result = {
                    "tool_id": tool_id,
                    "host": host,
                    "command": tool.get("base_command", ""),
                    "success": False,
                    "raw_output": "",
                    "parsed": {},
                    "error": str(e),
                }

            checked.append(tool_id)
            evidence.append({"tool": tool_id, "parsed": result.get("parsed", {})})

            outcome = _evaluate(tool_id, result, ctx)
            if outcome.get("decision") in ("root_cause", "healthy"):
                verdict = Verdict(True, outcome.get("cause"), outcome.get("fix"), checked, evidence)
                break
        if verdict:
            break
        intent = rule.get("next_intent_on_fail")   # escalate

    if not verdict:
        verdict = Verdict(
            False,
            "No root cause found by the rule chain.",
            "Escalate to a human or LLM.",
            checked,
            evidence,
        )

    # Optional LLM-assisted diagnosis enrichment
    engine = llm_engine
    if engine is None and (enable_llm or os.getenv("LLM_ENABLED", "false").strip().lower() in ("true", "1", "yes", "on")):
        engine = LLMEngine(enabled=True)

    if engine is not None:
        deterministic_result = {
            "resolved": verdict.resolved,
            "root_cause": verdict.root_cause,
            "suggested_fix": verdict.suggested_fix,
        }
        context = EvidenceBuilder.build_context(
            question=question,
            host=host,
            peer=peer,
            starting_intent=starting_intent or "unknown",
            checked=checked,
            evidence=evidence,
            deterministic_result=deterministic_result,
            telemetry=telemetry,
        )
        verdict.llm_diagnosis = engine.diagnose(context)

    return verdict