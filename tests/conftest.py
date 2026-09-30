"""Shared fixtures. No test touches the lab, Ollama, the Anthropic API, or the
repo's own rules.db / trained ML model."""
import pytest
from fastapi.testclient import TestClient

import api.main as api
import tools.config
from analyzer import llm_client, ml_engine
from rules_db.rules_db import RulesDB
from tests.fakes import FakeDeviceClient, LabClient, ScriptedLLM
from tests.scenarios import HOST, SCENARIOS


@pytest.fixture(scope="session", autouse=True)
def ml_model(tmp_path_factory):
    """Train the ML model once per test run into a temp dir (about a second),
    so tests never read or overwrite analyzer/models/."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ml_engine, "MODEL_PATH", tmp_path_factory.mktemp("models") / "model.joblib")
        mp.setattr(ml_engine, "_model", None)
        yield


@pytest.fixture(autouse=True)
def no_real_llm(monkeypatch):
    """Fail loudly if a test reaches the LLM without scripting its answers."""
    def refuse(*args, **kwargs):
        raise AssertionError("unexpected LLM call; use the scripted_llm fixture")
    monkeypatch.setattr(llm_client, "chat_json", refuse)


@pytest.fixture
def scripted_llm(monkeypatch):
    """scripted_llm(answer, ...) installs a ScriptedLLM and returns it."""
    def install(*answers, **kwargs) -> ScriptedLLM:
        llm = ScriptedLLM(*answers, **kwargs)
        monkeypatch.setattr(llm_client, "chat_json", llm)
        return llm
    return install


@pytest.fixture
def lab(monkeypatch, tmp_path):
    """lab("neighbor_shutdown") returns a LabClient whose tools see that scenario."""
    monkeypatch.setattr(api, "rules_db", RulesDB(tmp_path / "rules.db"))
    monkeypatch.setattr(tools.config, "BASELINE_DIR", tmp_path / "baselines")
    (tmp_path / "baselines").mkdir()

    def make(name: str) -> LabClient:
        scenario = SCENARIOS[name]
        device = FakeDeviceClient(scenario)
        for tool in (api.bgp_tool, api.interface_tool, api.tcp_tool, api.config_tool):
            monkeypatch.setattr(tool, "device_client", device)
        if "baseline" in scenario:
            api.config_tool.save_baseline(HOST, scenario["baseline"])
        return LabClient(TestClient(api.app), device)
    return make
