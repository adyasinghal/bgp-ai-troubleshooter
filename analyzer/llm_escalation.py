"""LLM escalation: hand a case the rules and ML engine couldn't resolve to an LLM.

The LLM gets the operator's question, every tool's output (parsed fields and
raw CLI text) and the ML engine's best guess, and returns a structured
diagnosis: root cause, fix, confidence, and the commands to run next.

Currently uses a local model served by Ollama (free, nothing leaves the machine).
Ollama runs on the Mac; from the OrbStack VM it is reached at host.orb.internal.
  BGP_LLM_URL      Ollama address        (default http://host.orb.internal:11434)
  BGP_LLM_MODEL    model to use          (default qwen2.5:7b; `ollama pull` it first)
  BGP_LLM_NUM_CTX  context window tokens (default 16384)
  BGP_LLM_TIMEOUT  seconds to wait       (default 300)

The Claude (Anthropic) version is commented out.
To switch back: uncomment it and `import anthropic`, comment out the Ollama
escalate(), and set ANTHROPIC_API_KEY.
"""
import json
import logging
import os

import requests
# import anthropic   # Claude version

OLLAMA_URL = os.environ.get("BGP_LLM_URL", "http://host.orb.internal:11434").rstrip("/")
MODEL = os.environ.get("BGP_LLM_MODEL", "qwen2.5:7b")
NUM_CTX = int(os.environ.get("BGP_LLM_NUM_CTX", "16384"))
TIMEOUT = int(os.environ.get("BGP_LLM_TIMEOUT", "300"))
# MODEL = os.environ.get("BGP_LLM_MODEL", "claude-opus-5")   # Claude version

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


class LLMUnavailable(Exception):
    """The LLM could not be reached or declined to answer."""


def escalate(question: str, host: str, peer: str | None, checked: list[str],
             evidence: list[dict], ml_prediction: dict | None = None) -> dict:
    """Ask the local Ollama model to diagnose the case. Returns a dict matching DIAGNOSIS_SCHEMA plus 'model'."""
    case = {
        "question": question,
        "host": host,
        "peer": peer,
        "tools_checked_in_order": checked,
        "ml_prediction": ml_prediction,
        "evidence": evidence,
    }
    log.info("Escalating to LLM (Ollama model %s at %s)", MODEL, OLLAMA_URL)
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=TIMEOUT, json={
            "model": MODEL,
            "stream": False,
            # Ollama constrains the output to this JSON schema.
            "format": DIAGNOSIS_SCHEMA,
            # temperature 0 keeps structured output stable; num_predict stops runaway output.
            "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": 2048},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": (
                    "Diagnose this case:\n\n" + json.dumps(case, indent=2, sort_keys=True)
                    + "\n\nReply with only a JSON object matching this schema:\n"
                    + json.dumps(DIAGNOSIS_SCHEMA))},
            ],
        })
    except requests.ConnectionError as e:
        raise LLMUnavailable(f"could not reach Ollama at {OLLAMA_URL} (is the Ollama app running?)") from e
    except requests.Timeout as e:
        raise LLMUnavailable(f"Ollama did not answer within {TIMEOUT}s") from e
    if r.status_code == 404:
        raise LLMUnavailable(f"model {MODEL} not found in Ollama; run `ollama pull {MODEL}` on the Mac")
    if not r.ok:
        raise LLMUnavailable(f"Ollama error {r.status_code}: {r.text[:200]}")

    data = r.json()
    input_tokens = data.get("prompt_eval_count")
    log.info("LLM response: model=%s done_reason=%s input_tokens=%s output_tokens=%s",
             data.get("model"), data.get("done_reason"), input_tokens, data.get("eval_count"))
    if input_tokens and input_tokens >= NUM_CTX:
        log.warning("Prompt filled the %s-token context window and was likely truncated; "
                    "raise BGP_LLM_NUM_CTX", NUM_CTX)
    if data.get("done_reason") == "length":
        raise LLMUnavailable("the model's answer was cut off")

    text = data["message"]["content"]
    log.debug("LLM diagnosis: %s", text)
    try:
        diagnosis = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMUnavailable("the model did not return valid JSON") from e
    missing = [k for k in DIAGNOSIS_SCHEMA["required"] if k not in diagnosis]
    if missing:
        raise LLMUnavailable(f"the model's answer is missing {', '.join(missing)}")
    return {**diagnosis, "model": data.get("model", MODEL)}


# --- Claude (Anthropic) version ------------------------------
# def escalate(question: str, host: str, peer: str | None, checked: list[str],
#              evidence: list[dict], ml_prediction: dict | None = None) -> dict:
#     """Ask Claude to diagnose the case. Returns a dict matching DIAGNOSIS_SCHEMA plus 'model'."""
#     case = {
#         "question": question,
#         "host": host,
#         "peer": peer,
#         "tools_checked_in_order": checked,
#         "ml_prediction": ml_prediction,
#         "evidence": evidence,
#     }
#     log.info("Escalating to LLM (model %s)", MODEL)
#     try:
#         client = anthropic.Anthropic()
#         response = client.beta.messages.create(
#             model=MODEL,
#             max_tokens=16000,
#             # If Claude's safety classifiers decline, retry on Anthropic's recommended fallback model.
#             betas=["server-side-fallback-2026-07-01"],
#             fallbacks="default",
#             system=SYSTEM_PROMPT,
#             output_config={"format": {"type": "json_schema", "schema": DIAGNOSIS_SCHEMA}},
#             messages=[{
#                 "role": "user",
#                 "content": "Diagnose this case:\n\n" + json.dumps(case, indent=2, sort_keys=True),
#             }],
#         )
#     except anthropic.AuthenticationError as e:
#         raise LLMUnavailable("Anthropic API key was rejected") from e
#     except anthropic.RateLimitError as e:
#         raise LLMUnavailable("Anthropic API rate limit hit; try again shortly") from e
#     except anthropic.APIStatusError as e:
#         raise LLMUnavailable(f"Anthropic API error {e.status_code}: {e.message}") from e
#     except anthropic.APIConnectionError as e:
#         raise LLMUnavailable("could not reach the Anthropic API") from e
#     except TypeError as e:
#         if "authentication" not in str(e):
#             raise
#         raise LLMUnavailable("no Anthropic credentials (set ANTHROPIC_API_KEY)") from e
#
#     log.info("LLM response: model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
#              response.model, response.stop_reason,
#              response.usage.input_tokens, response.usage.output_tokens)
#     if response.stop_reason == "refusal":
#         raise LLMUnavailable("the model declined to answer")
#     if response.stop_reason == "max_tokens":
#         raise LLMUnavailable("the model's answer was cut off")
#
#     text = next(b.text for b in response.content if b.type == "text")
#     log.debug("LLM diagnosis: %s", text)
#     return {**json.loads(text), "model": response.model}
