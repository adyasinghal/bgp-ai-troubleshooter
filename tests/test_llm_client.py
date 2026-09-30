"""llm_client.chat_json against mocked Ollama and Anthropic backends."""
import json
from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from analyzer import llm_client
from analyzer.llm_client import LLMUnavailable
from analyzer.llm_escalation import DIAGNOSIS_SCHEMA

ANSWER = {"resolved": True, "root_cause": "rc", "suggested_fix": "fx",
          "confidence": "high", "next_checks": []}


@pytest.fixture(autouse=True)
def real_chat_json(monkeypatch):
    """Undo conftest's no_real_llm guard: these tests call chat_json with the network mocked."""
    monkeypatch.setattr(llm_client, "chat_json", _REAL_CHAT_JSON)


_REAL_CHAT_JSON = llm_client.chat_json


def ollama_reply(content: str, done_reason: str = "stop", status: int = 200):
    r = mock.Mock(status_code=status, ok=status < 400, text="error body")
    r.json.return_value = {"model": "qwen2.5:7b", "done_reason": done_reason,
                           "prompt_eval_count": 100, "eval_count": 20,
                           "message": {"content": content}}
    return r


def call():
    return llm_client.chat_json("system", "user", DIAGNOSIS_SCHEMA)


def test_ollama_success(monkeypatch):
    monkeypatch.setattr(llm_client, "PROVIDER", "ollama")
    with mock.patch("requests.post", return_value=ollama_reply(json.dumps(ANSWER))) as post:
        assert call() == (ANSWER, "qwen2.5:7b")
    body = post.call_args.kwargs["json"]
    assert body["format"] == DIAGNOSIS_SCHEMA
    assert body["messages"][0] == {"role": "system", "content": "system"}
    assert body["messages"][1]["content"].startswith("user")


@pytest.mark.parametrize("reply, message", [
    (ollama_reply("not json"), "did not return valid JSON"),
    (ollama_reply("[1, 2]"), "did not return a JSON object"),
    (ollama_reply(json.dumps({"resolved": True})), "missing root_cause"),
    (ollama_reply("{}", done_reason="length"), "cut off"),
    (ollama_reply("", status=404), "not found in Ollama"),
    (ollama_reply("", status=500), "Ollama error 500"),
])
def test_ollama_bad_answers(monkeypatch, reply, message):
    monkeypatch.setattr(llm_client, "PROVIDER", "ollama")
    with mock.patch("requests.post", return_value=reply):
        with pytest.raises(LLMUnavailable, match=message):
            call()


@pytest.mark.parametrize("error, message", [
    (requests.ConnectionError(), "could not reach Ollama"),
    (requests.Timeout(), "did not answer within"),
])
def test_ollama_unreachable(monkeypatch, error, message):
    monkeypatch.setattr(llm_client, "PROVIDER", "ollama")
    with mock.patch("requests.post", side_effect=error):
        with pytest.raises(LLMUnavailable, match=message):
            call()


def anthropic_reply(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        model="claude-opus-5-5", stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
        content=[SimpleNamespace(type="text", text=text)])


def test_anthropic_success(monkeypatch):
    monkeypatch.setattr(llm_client, "PROVIDER", "anthropic")
    with mock.patch("anthropic.Anthropic") as client_cls:
        create = client_cls.return_value.beta.messages.create
        create.return_value = anthropic_reply(json.dumps(ANSWER))
        assert call() == (ANSWER, "claude-opus-5-5")
    kwargs = create.call_args.kwargs
    assert kwargs["system"] == "system"
    assert kwargs["output_config"]["format"]["schema"] == DIAGNOSIS_SCHEMA


def test_anthropic_refusal(monkeypatch):
    monkeypatch.setattr(llm_client, "PROVIDER", "anthropic")
    with mock.patch("anthropic.Anthropic") as client_cls:
        client_cls.return_value.beta.messages.create.return_value = anthropic_reply("", "refusal")
        with pytest.raises(LLMUnavailable, match="declined"):
            call()


def test_unknown_provider(monkeypatch):
    monkeypatch.setattr(llm_client, "PROVIDER", "bogus")
    with pytest.raises(LLMUnavailable, match="unknown BGP_LLM_PROVIDER"):
        call()
