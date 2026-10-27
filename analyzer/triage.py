"""Triage: read the question before any tool runs (keywords in rules mode, the LLM in agent mode)."""
import json
import logging
import re

from analyzer import llm_client, prompts

log = logging.getLogger(__name__)

PREFIX_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3}/\d{1,2})\b")
ROUTE_WORD_RE = re.compile(r"\broutes?\b|\bprefix(?:es)?\b|\bpaths?\b|\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}")


def triage(question: str) -> str:
    q = question.lower()
    if any(w in q for w in ("interface", "link", "cable")):
        return "interface_check"
    if any(w in q for w in ("tcp", "179", "reachab", "firewall")):
        return "tcp_port_check"
    if any(w in q for w in ("config", "mismatch", "remote-as", "as number")):
        return "config_check"
    return "bgp_state_check"   # default: start from BGP session state


def extract_prefix(question: str) -> str | None:
    """The first prefix (a.b.c.d/len) in the question, if any."""
    m = PREFIX_RE.search(question)
    return m.group(1) if m else None


def route_intent(question: str) -> str | None:
    """The route rule a question is about, or None for a session question.

    The rules engine still checks the session first (bgp_state) and only moves
    to this rule when the session is Established.
    """
    q = question.lower()
    about_routes = bool(ROUTE_WORD_RE.search(q))
    if re.search(r"best[ -]?path|which path|path selection|preferred path|tie-?break"
                 r"|why\b.*\b(?:prefers?|preferred|chooses?|chosen|selects?|selected|picks?|picked|wins?|winning)\b", q):
        return "route_best_path_selection"
    if about_routes and re.search(r"not (?:being )?(?:selected|chosen|preferred|used|best|installed|received|"
                                  r"learn(?:ed|t)?|showing|there|in the table)|isn'?t (?:selected|chosen|there)"
                                  r"|(?:not|n't) (?:\w+ )?(?:receiv|learn|get|see|accept)\w*"
                                  r"|\bno (?:routes?|prefix(?:es)?|paths?)\b|\bzero (?:routes|prefixes)\b"
                                  r"|missing|disappear|gone|lost|filtered|dropped|withdrawn|denied|rejected", q):
        return "route_received_not_selected"
    if re.search(r"route-?map|route map|prefix-?list|prefix list|filter-?list", q):
        return "route_map_check"
    if about_routes or re.search(r"local[- ]?pref|\bmed\b|as[- ]?path|next[- ]?hop|\bweight\b", q):
        return "route_leading_case"
    return None


def route_context(question: str) -> dict:
    """The route-question keys the rules engine reads from ctx."""
    intent, prefix = route_intent(question), extract_prefix(question)
    return {"route_question": bool(intent or prefix),
            "route_intent": intent or ("route_received_not_selected" if prefix else None),
            "prefix": prefix}


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
