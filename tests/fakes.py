"""Fakes for SSH (FakeDeviceClient), the REST client (LabClient, backed by the
in-process API) and the LLM (ScriptedLLM)."""
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


def call(tool_id: str, intent: str = "", thought: str = "", **args) -> dict:
    return {"thought": thought or f"check {tool_id}", "action": "call_tool",
            "intent": intent, "tool_id": tool_id, "args": args}


def conclude(fault_class: str, root_cause: str = "", resolved: bool = True,
             confidence: str = "high", fix: str = "fix it", next_checks=()) -> dict:
    return {"thought": "enough evidence", "action": "conclude", "diagnosis": {
        "resolved": resolved, "root_cause": root_cause or fault_class, "fault_class": fault_class,
        "suggested_fix": fix, "confidence": confidence, "next_checks": list(next_checks)}}


def triage_answer(in_scope: bool = True, claimed_state: str = "none", interface: str = "",
                  hypotheses=(), plan=("bgp_state_check",)) -> dict:
    return {"in_scope": in_scope, "symptom": "peer is down", "claimed_state": claimed_state,
            "interface": interface, "plan": list(plan),
            "hypotheses": [{"fault_class": h, "why": "a guess"} for h in hypotheses]}


class ScriptedLLM:
    """Stands in for llm_client.chat_json; returns (or raises) queued answers in order.
    Triage calls get `triage` and are recorded separately."""

    def __init__(self, *answers, triage=None, model: str = "scripted-llm"):
        self.answers = list(answers)
        self.triage = triage or triage_answer()
        self.model = model
        self.calls: list[dict] = []
        self.triage_calls: list[dict] = []

    def __call__(self, system: str, user: str, schema: dict, max_tokens: int = 2048):
        if "hypotheses" in schema["properties"]:
            self.triage_calls.append({"system": system, "user": user, "schema": schema})
            if isinstance(self.triage, Exception):
                raise self.triage
            return self.triage, self.model
        self.calls.append({"system": system, "user": user, "schema": schema})
        if not self.answers:
            raise AssertionError("ScriptedLLM ran out of answers")
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, self.model
