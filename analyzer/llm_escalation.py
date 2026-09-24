"""LLM escalation: hand a case the rules and ML engine couldn't resolve to Claude.

Claude gets the operator's question, every tool's output (parsed fields and
raw CLI text) and the ML engine's best guess, and returns a structured
diagnosis: root cause, fix, confidence, and the commands to run next.

Needs Anthropic credentials: set ANTHROPIC_API_KEY (or run `ant auth login`).
Override the model with the BGP_LLM_MODEL environment variable.
"""
import json
import os

import anthropic

MODEL = os.environ.get("BGP_LLM_MODEL", "claude-opus-5")

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


class LLMUnavailable(Exception):
    """The LLM could not be reached or declined to answer."""


def escalate(question: str, host: str, peer: str | None, checked: list[str],
             evidence: list[dict], ml_prediction: dict | None = None) -> dict:
    """Ask Claude to diagnose the case. Returns a dict matching DIAGNOSIS_SCHEMA plus 'model'."""
    case = {
        "question": question,
        "host": host,
        "peer": peer,
        "tools_checked_in_order": checked,
        "ml_prediction": ml_prediction,
        "evidence": evidence,
    }
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            # If Claude's safety classifiers decline, retry on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=SYSTEM_PROMPT,
            output_config={"format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA}},
            messages=[{
                "role": "user",
                "content": "Diagnose this case:\n\n" + json.dumps(case, indent=2, sort_keys=True),
            }],
        )
    except anthropic.AuthenticationError as e:
        raise LLMUnavailable("Anthropic API key was rejected") from e
    except anthropic.RateLimitError as e:
        raise LLMUnavailable("Anthropic API rate limit hit; try again shortly") from e
    except anthropic.APIStatusError as e:
        raise LLMUnavailable(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMUnavailable("could not reach the Anthropic API") from e
    except TypeError as e:
        if "authentication" not in str(e):
            raise
        raise LLMUnavailable("no Anthropic credentials (set ANTHROPIC_API_KEY)") from e

    if response.stop_reason == "refusal":
        raise LLMUnavailable("the model declined to answer")
    if response.stop_reason == "max_tokens":
        raise LLMUnavailable("the model's answer was cut off")

    text = next(b.text for b in response.content if b.type == "text")
    return {**json.loads(text), "model": response.model}
