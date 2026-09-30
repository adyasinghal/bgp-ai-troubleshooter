"""Stand-ins for the lab and the LLM, so tests run without Containerlab or Ollama.

- FakeDeviceClient replaces SSH: it answers each command with a scenario's
  canned FRR output (see scenarios.py).
- LabClient has the same methods as analyzer.rest_client.RestClient, but is
  served by the real REST API (api/main.py) running in-process.
- ScriptedLLM replaces llm_client.chat_json with a queue of prepared answers.

conftest.py wires these up as the `lab` and `scripted_llm` fixtures.
"""
import re

from fastapi.testclient import TestClient

from tools.device_client import DeviceResult


class FakeDeviceClient:
    def __init__(self, scenario: dict):
        self.scenario = scenario
        self.commands: list[tuple[str, str]] = []   # (host, command) in call order

    def run_vtysh(self, host: str, command: str) -> DeviceResult:
        self.commands.append((host, command))
        output = self.scenario.get(command)
        if output is None and command.startswith("show interface "):
            output = self._one_interface(command.split()[-1])
        return self._answer(host, command, output)

    def run_raw(self, host: str, command: str) -> DeviceResult:
        self.commands.append((host, command))
        return self._answer(host, command, self.scenario.get("tcp" if "/dev/tcp/" in command else command))

    def _one_interface(self, name: str) -> str | None:
        """`show interface <name>`: that interface's block from the full `show interface`."""
        for block in re.split(r"\n(?=Interface \S+ is )", self.scenario.get("show interface", "")):
            if block.startswith(f"Interface {name} is "):
                return block
        return f"% Can't find interface {name}"

    def _answer(self, host: str, command: str, output: str | None) -> DeviceResult:
        if self.scenario.get("unreachable"):
            return DeviceResult(host, command, False, "",
                                f"NoValidConnectionsError: Unable to connect to port 22 on {host}")
        if output is None:
            return DeviceResult(host, command, False, "", f"no canned output for {command!r}")
        return DeviceResult(host, command, True, output)


class LabClient:
    """Drop-in for RestClient that calls the in-process API instead of HTTP."""

    def __init__(self, http: TestClient, device: FakeDeviceClient):
        self.http = http
        self.device = device

    def get_rule(self, intent: str) -> dict:
        r = self.http.get(f"/rules/{intent}")
        r.raise_for_status()
        return r.json()

    def get_catalog(self) -> dict:
        r = self.http.get("/rules/catalog")
        r.raise_for_status()
        return r.json()

    def call_tool(self, endpoint: str, payload: dict) -> dict:
        r = self.http.post(endpoint, json=payload)
        r.raise_for_status()
        return r.json()


class ScriptedLLM:
    """Replaces llm_client.chat_json. Each call returns the next queued answer;
    a queued Exception is raised instead. Every call is recorded in `calls`."""

    def __init__(self, *answers, model: str = "scripted-llm"):
        self.answers = list(answers)
        self.model = model
        self.calls: list[dict] = []

    def __call__(self, system: str, user: str, schema: dict, max_tokens: int = 2048):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if not self.answers:
            raise AssertionError("ScriptedLLM ran out of answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, self.model
