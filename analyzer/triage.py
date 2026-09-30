"""Triage: turn a plain-language question into a starting intent."""
import json
import logging

from analyzer import llm_client, prompts

log = logging.getLogger(__name__)


def triage(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ("interface", "link", "cable")):
        return "interface_check"
    if any(w in q for w in ("tcp", "179", "reachab", "firewall")):
        return "tcp_port_check"
    if any(w in q for w in ("config", "mismatch", "remote-as", "as number")):
        return "config_check"
    return "bgp_state_check"   # default: start from BGP session state


def llm_triage(question: str, host: str, peer: str | None, registry) -> dict:
    """Have the LLM read the question before any tool runs."""
    intents = registry.intents()
    case = {
        "question": question,
        "host": host,
        "peer": peer,
        "rules": [{"intent": r["intent"], "symptoms": r["symptoms"]} for r in registry.rules],
        "fault_classes": prompts.FAULT_CLASSES,
    }
    answer, _ = llm_client.chat_json(prompts.TRIAGE_PROMPT, json.dumps(case, indent=1),
                                     prompts.triage_schema(intents), max_tokens=512)
    plan = [i for i in answer.get("plan", []) if i in intents] or [triage(question)]
    result = {
        "method": "llm",
        "in_scope": bool(answer["in_scope"]),
        "symptom": answer["symptom"],
        "claimed_state": answer["claimed_state"],
        "interface": answer.get("interface") or None,
        "hypotheses": [h for h in answer.get("hypotheses", [])
                       if h.get("fault_class") in prompts.FAULT_CLASSES][:4],
        "plan": plan,
    }
    log.info("Triage: %s", result)
    return result
