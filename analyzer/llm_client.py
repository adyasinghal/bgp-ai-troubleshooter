"""LLM client: the one place that talks to the model backend.

Every LLM call in the analyzer (escalation today, triage and the agent loop
next) goes through chat_json(): send a system prompt and a user message, get
back a dict that matches a JSON schema. Callers never see which backend
answered, so switching between a local model and Claude is a config change.

Settings (environment variables):
  BGP_LLM_PROVIDER  ollama | anthropic                (default ollama)
  BGP_LLM_MODEL     model to use                      (default qwen2.5:7b for ollama,
                                                       claude-opus-5-5 for anthropic)
  BGP_LLM_URL       Ollama address                    (default http://host.orb.internal:11434)
  BGP_LLM_NUM_CTX   Ollama context window tokens      (default 16384)
  BGP_LLM_TIMEOUT   seconds to wait for Ollama        (default 300)

ollama: a free local model; nothing leaves the machine. Ollama runs on the
Mac; from the OrbStack VM it is reached at host.orb.internal.
anthropic: Claude via the Anthropic API; needs ANTHROPIC_API_KEY.
"""
import json
import logging
import os

import requests

PROVIDER = os.environ.get("BGP_LLM_PROVIDER", "ollama").lower()
DEFAULT_MODELS = {"ollama": "qwen2.5:7b", "anthropic": "claude-opus-5-5"}
MODEL = os.environ.get("BGP_LLM_MODEL", DEFAULT_MODELS.get(PROVIDER, ""))
OLLAMA_URL = os.environ.get("BGP_LLM_URL", "http://host.orb.internal:11434").rstrip("/")
NUM_CTX = int(os.environ.get("BGP_LLM_NUM_CTX", "16384"))
TIMEOUT = int(os.environ.get("BGP_LLM_TIMEOUT", "300"))

log = logging.getLogger(__name__)


class LLMUnavailable(Exception):
    """The LLM could not be reached, declined to answer, or gave an unusable answer."""


def describe() -> str:
    """Short label for logs, e.g. 'ollama model qwen2.5:7b at http://...'."""
    if PROVIDER == "ollama":
        return f"ollama model {MODEL} at {OLLAMA_URL}"
    return f"{PROVIDER} model {MODEL}"


def chat_json(system: str, user: str, schema: dict, max_tokens: int = 2048) -> tuple[dict, str]:
    """Ask the configured model for a JSON object matching `schema`.

    Returns (answer, model name). Raises LLMUnavailable on any failure, so
    callers can fall back without caring why the LLM didn't answer.
    """
    if PROVIDER == "ollama":
        text, model = _ollama(system, user, schema, max_tokens)
    elif PROVIDER == "anthropic":
        text, model = _anthropic(system, user, schema, max_tokens)
    else:
        raise LLMUnavailable(f"unknown BGP_LLM_PROVIDER {PROVIDER!r} (use ollama or anthropic)")

    log.debug("LLM answer: %s", text)
    try:
        answer = json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMUnavailable("the model did not return valid JSON") from e
    if not isinstance(answer, dict):
        raise LLMUnavailable("the model did not return a JSON object")
    missing = [k for k in schema.get("required", []) if k not in answer]
    if missing:
        raise LLMUnavailable(f"the model's answer is missing {', '.join(missing)}")
    return answer, model


def _ollama(system: str, user: str, schema: dict, max_tokens: int) -> tuple[str, str]:
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat", timeout=TIMEOUT, json={
            "model": MODEL,
            "stream": False,
            # Ollama constrains the output to this JSON schema.
            "format": schema,
            # temperature 0 keeps structured output stable; num_predict stops runaway output.
            "options": {"temperature": 0, "num_ctx": NUM_CTX, "num_predict": max_tokens},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": (
                    user + "\n\nReply with only a JSON object matching this schema:\n"
                    + json.dumps(schema))},
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
    return data["message"]["content"], data.get("model", MODEL)


def _anthropic(system: str, user: str, schema: dict, max_tokens: int) -> tuple[str, str]:
    import anthropic   # only needed for this provider

    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            # If Claude's safety classifiers decline, retry on Anthropic's recommended fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": user}],
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

    log.info("LLM response: model=%s stop_reason=%s input_tokens=%s output_tokens=%s",
             response.model, response.stop_reason,
             response.usage.input_tokens, response.usage.output_tokens)
    if response.stop_reason == "refusal":
        raise LLMUnavailable("the model declined to answer")
    if response.stop_reason == "max_tokens":
        raise LLMUnavailable("the model's answer was cut off")
    return next(b.text for b in response.content if b.type == "text"), response.model
