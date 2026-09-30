"""LLM escalation: hand a case the rules and ML engine couldn't resolve to an LLM.

The LLM gets the operator's question, every tool's output (parsed fields and
raw CLI text) and the ML engine's best guess, and returns a structured
diagnosis: root cause, fix, confidence, and the commands to run next.

Which model answers (local Ollama or Claude) is set in analyzer/llm_client.py.
"""
import json
import logging

from analyzer import llm_client
from analyzer.llm_client import LLMUnavailable   # re-exported for callers

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a senior network engineer troubleshooting BGP sessions on FRRouting (FRR) routers running in a Containerlab lab.

An automated troubleshooter ran diagnostic tools against one router but could not pin down the root cause with its rule chain or its ML classifier. You are given the operator's question, the output of every tool that ran (parsed fields plus the raw vtysh/shell output), and the ML classifier's best guess with its confidence. The classifier was trained mostly on synthetic data, so treat its guess as a hint, not a finding.

Common causes of a stuck session include a remote-as mismatch, a neighbor configured on only one side, an administratively shut down neighbor, a wrong neighbor IP or update-source, no route to the peer, eBGP multihop/TTL problems, an MD5 password mismatch, and a router-id conflict. The tools only inspect one router, so the fault may be on the peer; say so when the evidence points there.

Set resolved to true only when the evidence supports a specific root cause. Otherwise set it to false, give your best hypothesis as root_cause, and use next_checks for the exact commands (and which router to run them on) that would confirm it. Write suggested_fix as exact FRR commands where possible."""

DIAGNOSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "resolved": {"type": "boolean"},
        "root_cause": {"type": "string"},
        "suggested_fix": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "next_checks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["resolved", "root_cause", "suggested_fix", "confidence", "next_checks"],
    "additionalProperties": False,
}


def escalate(question: str, host: str, peer: str | None, checked: list[str],
             evidence: list[dict], ml_prediction: dict | None = None) -> dict:
    """Ask the LLM to diagnose the case. Returns a dict matching DIAGNOSIS_SCHEMA plus 'model'."""
    case = {
        "question": question,
        "host": host,
        "peer": peer,
        "tools_checked_in_order": checked,
        "ml_prediction": ml_prediction,
        "evidence": evidence,
    }
    log.info("Escalating to LLM (%s)", llm_client.describe())
    diagnosis, model = llm_client.chat_json(
        SYSTEM_PROMPT,
        "Diagnose this case:\n\n" + json.dumps(case, indent=2, sort_keys=True),
        DIAGNOSIS_SCHEMA,
    )
    return {**diagnosis, "model": model}
