"""Tool registry: the tools the analyzer may call, and the one place that
validates and runs a call.

Built from the Rules DB catalog (GET /rules/catalog), so a new device tool is
a catalog row plus a REST endpoint, with no change here. Internal tools (the
ML classifier) run in-process instead of over REST.

Arguments come from two places:
  - context_args: host/peer from the run itself (CLI flags). Always applied,
    never taken from the LLM, so it can't point a tool at another device.
  - args_schema: the few args the LLM may choose (e.g. which interface),
    checked by validate() before anything runs.
"""
import logging
import re

import requests

from analyzer import ml_engine

log = logging.getLogger(__name__)

# Keys the LLM may send out of habit; they're filled from the run context instead.
CONTEXT_KEYS = {"host", "peer", "peer_ip"}

TYPES = {"string": str, "integer": int, "boolean": bool}


class ToolCallError(ValueError):
    """The requested call is not allowed: unknown tool or invalid arguments."""


class ToolRegistry:
    def __init__(self, tools: list[dict], rules: list[dict]):
        self.tools = {t["tool_id"]: t for t in tools}
        self.rules = rules

    @classmethod
    def load(cls, client) -> "ToolRegistry":
        catalog = client.get_catalog()
        return cls(catalog["tools"], catalog["rules"])

    def remove(self, tool_id: str):
        """Take a tool out of this run's catalog (e.g. ml_classify with --no-ml)."""
        self.tools.pop(tool_id, None)
        self.rules = [r for r in self.rules if r["tool_id"] != tool_id]

    # --- what the LLM sees ---

    def all_args(self) -> dict:
        """Every tool's LLM-settable args in one dict (for the decision schema)."""
        merged = {}
        for t in self.tools.values():
            merged.update(t["args_schema"])
        return merged

    def intents(self) -> list[str]:
        return sorted({r["intent"] for r in self.rules})

    def next_intent_after(self, tool_id: str) -> str | None:
        """What the rule book would try next if this tool's result doesn't resolve the case."""
        for r in sorted(self.rules, key=lambda r: r["priority"]):
            if r["tool_id"] == tool_id and r.get("next_intent_on_fail"):
                return r["next_intent_on_fail"]
        return None

    def for_prompt(self) -> list[dict]:
        """Compact catalog for the LLM: each tool, the args it may set, and the rules that use it."""
        return [{
            "tool_id": t["tool_id"],
            "description": t["description"],
            "when_to_use": t["when_to_use"],
            "args": t["args_schema"],
            "rules": [{"intent": r["intent"], "symptoms": r["symptoms"],
                       "next_intent_on_fail": r["next_intent_on_fail"]}
                      for r in self.rules if r["tool_id"] == t["tool_id"]],
        } for t in self.tools.values()]

    # --- checking and running a call ---

    def validate(self, tool_id: str, args: dict | None) -> dict:
        """Return the cleaned args, or raise ToolCallError saying what's wrong."""
        tool = self.tools.get(tool_id)
        if tool is None:
            raise ToolCallError(f"unknown tool {tool_id!r}; choose one of {', '.join(self.tools)}")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ToolCallError(f"args for {tool_id} must be an object, got {type(args).__name__}")

        schema = tool["args_schema"]
        clean = {}
        for name, value in args.items():
            if name in CONTEXT_KEYS or name in tool["context_args"]:
                continue   # always filled from the run context
            if name not in schema:
                allowed = ", ".join(schema) or "none"
                raise ToolCallError(f"{tool_id} has no argument {name!r} (allowed: {allowed})")
            if value is None:
                continue
            spec = schema[name]
            expected = TYPES.get(spec.get("type", "string"), str)
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ToolCallError(f"{tool_id}.{name} must be a {spec.get('type', 'string')}")
            if "pattern" in spec and not re.fullmatch(spec["pattern"], str(value)):
                raise ToolCallError(f"{tool_id}.{name} value {value!r} is not allowed")
            clean[name] = value
        missing = [n for n, s in schema.items() if s.get("required") and n not in clean]
        if missing:
            raise ToolCallError(f"{tool_id} needs {', '.join(missing)}")
        return clean

    def payload(self, tool_id: str, args: dict, ctx: dict) -> dict:
        """Request body: context values (host/peer) plus the validated args."""
        tool = self.tools[tool_id]
        body = {field: ctx.get(key) for field, key in tool["context_args"].items()}
        body.update(args)
        return {k: v for k, v in body.items() if v is not None}

    def execute(self, client, tool_id: str, args: dict, ctx: dict,
                evidence: list[dict] | None = None) -> dict:
        """Run a validated call. Returns a tool result dict (the API's shape);
        failures come back as success=False instead of raising."""
        tool = self.tools[tool_id]
        if tool["kind"] == "internal":
            return self._internal(tool_id, ctx, evidence or [])

        body = self.payload(tool_id, args, ctx)
        log.info("Calling tool %s: POST %s %s", tool_id, tool["endpoint"], body)
        try:
            return client.call_tool(tool["endpoint"], body)
        except requests.RequestException as e:
            log.warning("Tool %s request failed: %s", tool_id, e)
            return _failed(tool_id, ctx, tool["base_command"], f"{type(e).__name__}: {e}")

    def _internal(self, tool_id: str, ctx: dict, evidence: list[dict]) -> dict:
        if tool_id != "ml_classify":
            return _failed(tool_id, ctx, tool_id, f"no internal implementation for {tool_id}")
        device_evidence = [e for e in evidence if self.tools.get(e["tool"], {}).get("kind") == "device"]
        if not device_evidence:
            return _failed(tool_id, ctx, tool_id, "no device tool output to classify yet")
        prediction = ml_engine.predict(device_evidence, ctx.get("peer"))
        cause, fix = prediction.describe(ctx.get("peer"))
        log.info("ML classify: %s (%.2f)", prediction.label, prediction.confidence)
        return {
            "tool_id": tool_id, "host": ctx.get("host"), "command": "ml classify",
            "success": True, "raw_output": "", "error": None,
            "parsed": {**prediction.to_dict(), "root_cause": cause, "suggested_fix": fix,
                       "tools_classified": [e["tool"] for e in device_evidence]},
        }


def _failed(tool_id: str, ctx: dict, command: str, error: str) -> dict:
    return {"tool_id": tool_id, "host": ctx.get("host"), "command": command,
            "success": False, "raw_output": "", "parsed": {}, "error": error}
