"""LLM agent: the LLM drives the investigation from the first step.

    triage -> [LLM decides -> guardrails -> tool -> rule finding] x N -> LLM concludes -> Verdict

The LLM picks each tool from the Rules DB catalog and reads every result
together with the rule book's deterministic finding for it (rules_engine.evaluate).
Code, not the prompt, enforces the limits: only catalogued tools, validated
args, no repeated calls, bgp_state before other tools, at least one tool
before concluding, and a step budget. If the LLM can't be reached or gives an unusable answer, the run falls
back to the rule chain (rules_engine.diagnose).
"""
import json
import logging

from analyzer import llm_client, ml_engine, prompts, rules_engine
from analyzer.llm_client import LLMUnavailable
from analyzer.tool_registry import ToolCallError, ToolRegistry
from analyzer.triage import triage
from analyzer.verdict import Verdict

DEFAULT_MAX_STEPS = 6
RAW_OUTPUT_LIMIT = 2500   # chars of raw CLI output the LLM sees per tool call
# Any other tool before this one is refused: the operator's description of the
# state can be wrong, and a small model otherwise acts on it without checking.
FIRST_TOOL = "bgp_state"

# A decided rule finding agrees with these LLM fault classes. config_drift is
# generic, so naming the specific config fault behind the diff also agrees.
AGREES_WITH = {
    "config_drift": {"config_drift", "remote_as_mismatch", "neighbor_shutdown", "neighbor_missing"},
}

log = logging.getLogger(__name__)


def investigate(client, question: str, host: str, peer: str | None,
                max_steps: int = DEFAULT_MAX_STEPS, use_ml: bool = True,
                trust_rules: bool = False) -> Verdict:
    """Run the LLM-driven investigation; fall back to the rule chain without an LLM."""
    registry = ToolRegistry.load(client)
    if not use_ml:
        registry.remove("ml_classify")
    run = Investigation(client, registry, question, {"host": host, "peer": peer},
                        max_steps, use_ml, trust_rules)
    try:
        return run.run()
    except LLMUnavailable as e:
        where = f" after {len(run.steps)} step(s)" if run.steps else ""
        log.warning("LLM agent unavailable%s: %s; falling back to the rule chain", where, e)
        verdict = rules_engine.diagnose(client, question, host, peer, use_ml=use_ml, use_llm=False)
        verdict.notes.append(f"LLM agent unavailable{where} ({e}); fell back to the rule chain")
        return verdict


class Investigation:
    def __init__(self, client, registry: ToolRegistry, question: str, ctx: dict,
                 max_steps: int, use_ml: bool, trust_rules: bool):
        self.client = client
        self.registry = registry
        self.question = question
        self.ctx = ctx
        self.max_steps = max_steps
        self.use_ml = use_ml
        self.trust_rules = trust_rules
        self.model = None
        # Stand-in until the LLM triage (Phase 2): keyword match -> suggested first intent.
        self.triage = {"method": "keyword", "suggested_first_intent": triage(question)}
        self.steps: list[dict] = []      # every step, including rejected ones
        self.evidence: list[dict] = []   # tool outputs, same shape as the rule chain's
        self.checked: list[str] = []
        self.findings: list[dict] = []   # rule findings, one per tool call
        self.calls: dict[tuple, int] = {}   # (tool_id, args[, evidence count]) -> step n

    def run(self) -> Verdict:
        log.info("Agent start: host=%s peer=%s max_steps=%d triage=%s",
                 self.ctx["host"], self.ctx["peer"], self.max_steps, self.triage)
        for n in range(1, self.max_steps + 1):
            decision = self._decide(steps_left=self.max_steps - n + 1)
            action = decision.get("action")
            if action == "conclude":
                problem = self._conclusion_problem(decision)
                if problem is None:
                    return self._verdict(decision)
                self._reject(n, decision, problem)
            elif action == "call_tool":
                finding = self._call(n, decision)
                if self.trust_rules and finding and finding["decision"] != "continue":
                    return self._rules_verdict(finding)
            else:
                self._reject(n, decision, f"action must be call_tool or conclude, not {action!r}")
        log.info("Agent used all %d steps; asking for a conclusion", self.max_steps)
        return self._verdict(self._decide(steps_left=0))

    # --- talking to the LLM ---

    def _decide(self, steps_left: int) -> dict:
        schema = (prompts.step_schema(self.registry.all_args()) if steps_left
                  else prompts.conclude_schema())
        user = prompts.step_message(self._case(), steps_left)
        log.debug("Agent prompt:\n%s", user)
        decision, self.model = llm_client.chat_json(prompts.SYSTEM_PROMPT, user, schema, max_tokens=1024)
        log.info("Agent decision: action=%s tool=%s args=%s intent=%s thought=%s",
                 decision.get("action"), decision.get("tool_id"), decision.get("args"),
                 decision.get("intent"), decision.get("thought"))
        return decision

    def _case(self) -> dict:
        return {
            "question": self.question,
            "host": self.ctx["host"],
            "peer": self.ctx["peer"],
            "triage": self.triage,
            "fault_classes": prompts.FAULT_CLASSES,
            "tools": self.registry.for_prompt(),
            "steps": [_step_for_prompt(s) for s in self.steps],
        }

    # --- one step ---

    def _call(self, n: int, decision: dict) -> dict | None:
        tool_id = decision.get("tool_id") or ""
        try:
            args = self.registry.validate(tool_id, decision.get("args"))
            key = (tool_id, json.dumps(args, sort_keys=True))
            if self.registry.tools[tool_id]["kind"] == "internal":
                key += (len(self._device_evidence()),)   # new device output -> a new answer
            if key in self.calls:
                raise ToolCallError(f"{tool_id} was already called with these args in step "
                                    f"{self.calls[key]}; use that result")
            if (tool_id != FIRST_TOOL and FIRST_TOOL in self.registry.tools
                    and FIRST_TOOL not in self.checked):
                raise ToolCallError(f"call {FIRST_TOOL} first: the session's actual state decides "
                                    f"which checks make sense")
        except ToolCallError as e:
            self._reject(n, decision, str(e))
            return None

        self.calls[key] = n
        result = self.registry.execute(self.client, tool_id, args, self.ctx, self.evidence)
        finding = rules_engine.evaluate(tool_id, result, self.ctx)
        if finding["decision"] == "continue":
            # next_intent_on_fail only applies while the case is unresolved
            finding["suggested_next_intent"] = self.registry.next_intent_after(tool_id)
        else:
            finding["note"] = ("The rule book considers the case decided by this result. "
                               "Conclude now unless other evidence already contradicts it.")
        summary = summarize(tool_id, result)
        if finding["decision"] != "continue":
            summary += f" [rule: {finding['decision']}]"
        log.info("Step %d: %s %s -> %s", n, tool_id, args or "", summary)
        log.debug("Step %d parsed output: %s", n, json.dumps(result.get("parsed"), sort_keys=True, default=str))
        log.debug("Step %d raw output:\n%s", n, result.get("raw_output"))

        self.checked.append(tool_id)
        self.findings.append(finding)
        self.evidence.append({"tool": tool_id, "success": result.get("success"),
                              "error": result.get("error"), "parsed": result.get("parsed"),
                              "raw_output": result.get("raw_output")})
        self.steps.append({"n": n, "thought": decision.get("thought"), "intent": decision.get("intent"),
                           "tool": tool_id, "args": args, "summary": summary,
                           "result": result, "rule_finding": finding})
        return finding

    def _reject(self, n: int, decision: dict, reason: str):
        log.warning("Step %d rejected: %s", n, reason)
        self.steps.append({"n": n, "thought": decision.get("thought"),
                           "tool": decision.get("tool_id"), "args": decision.get("args"),
                           "rejected": reason})

    def _conclusion_problem(self, decision: dict) -> str | None:
        if not self.evidence:
            return "call at least one tool before concluding; there is no evidence yet"
        missing = _missing_diagnosis_fields(decision)
        if missing:
            return f"a conclusion needs a complete diagnosis (missing {', '.join(missing)})"
        return None

    # --- verdicts ---

    def _verdict(self, decision: dict) -> Verdict:
        missing = _missing_diagnosis_fields(decision)
        if missing:
            raise LLMUnavailable(f"the model's conclusion is missing {', '.join(missing)}")
        d = decision["diagnosis"]
        notes = [f"Diagnosed by {self.model}"]
        resolved = bool(d["resolved"])
        if not self.evidence:
            resolved = False
            notes.append("Every tool call was rejected, so the conclusion has no evidence behind it")

        decided = [f for f in self.findings if f["decision"] != "continue"]
        rules_agree = (any(d["fault_class"] in AGREES_WITH.get(f["fault_class"], {f["fault_class"]})
                           for f in decided) if decided else None)
        if rules_agree is False:
            log.warning("Agent's fault class %s disagrees with the rule findings %s",
                        d["fault_class"], [f["fault_class"] for f in decided])
        log.info("Agent concluded: resolved=%s fault_class=%s confidence=%s rules_agree=%s root cause: %s",
                 resolved, d["fault_class"], d["confidence"], rules_agree, d["root_cause"])

        return Verdict(resolved, d["root_cause"], d["suggested_fix"], self.checked, self.evidence,
                       source="agent", confidence=d["confidence"], ml_prediction=self._ml_opinion(),
                       next_checks=list(d["next_checks"]), notes=notes,
                       fault_class=d["fault_class"], rules_agree=rules_agree,
                       triage=self.triage, steps=self._public_steps())

    def _rules_verdict(self, finding: dict) -> Verdict:
        log.info("Rule finding %s after %s; stopping (--trust-rules)", finding["decision"], self.checked[-1])
        return Verdict(True, finding["cause"], finding["fix"], self.checked, self.evidence,
                       source="rules", ml_prediction=self._ml_opinion(),
                       notes=["Stopped at the first rule finding (--trust-rules)"],
                       fault_class=finding["fault_class"], triage=self.triage,
                       steps=self._public_steps())

    def _device_evidence(self) -> list[dict]:
        return [e for e in self.evidence
                if self.registry.tools.get(e["tool"], {}).get("kind") == "device"]

    def _ml_opinion(self) -> dict | None:
        """ML second opinion over the device tools' output, shown next to the verdict."""
        device = self._device_evidence()
        if not (self.use_ml and device):
            return None
        ml = ml_engine.predict(device, self.ctx["peer"])
        log.info("ML second opinion: %s (%.2f)", ml.label, ml.confidence)
        return ml.to_dict()

    def _public_steps(self) -> list[dict]:
        keys = ("n", "thought", "intent", "tool", "args", "summary", "rejected")
        return [{k: s[k] for k in keys if k in s} for s in self.steps]


def _missing_diagnosis_fields(decision: dict) -> list[str]:
    d = decision.get("diagnosis")
    required = prompts.diagnosis_schema()["required"]
    if not isinstance(d, dict):
        return ["diagnosis"]
    return [k for k in required if k not in d]


def _step_for_prompt(step: dict) -> dict:
    """What the LLM sees of a step: parsed output, trimmed raw output, the rule finding."""
    if "rejected" in step:
        return {"n": step["n"], "tool": step["tool"], "args": step["args"], "rejected": step["rejected"]}
    r = step["result"]
    parsed = r.get("parsed") or {}
    if step["tool"] == "config" and not parsed.get("has_baseline"):
        # Without a baseline the diff is the whole config again, marked "+".
        parsed = {**parsed, "diff": "(no baseline saved, so no diff; the running config is in raw_output)"}
    shown = {"success": r.get("success"), "error": r.get("error"), "parsed": parsed}
    if r.get("raw_output"):
        shown["raw_output"] = _trim(r["raw_output"])
    return {"n": step["n"], "thought": step["thought"], "intent": step["intent"], "tool": step["tool"],
            "args": step["args"], "result": shown, "rule_finding": step["rule_finding"]}


def _trim(text: str) -> str:
    limit = RAW_OUTPUT_LIMIT
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters cut]"


def summarize(tool_id: str, result: dict) -> str:
    """One line per tool result, for the step trace and the log."""
    if not result.get("success", True):
        return f"failed: {result.get('error')}"
    p = result.get("parsed") or {}
    if tool_id == "bgp_state":
        reason = p.get("queried_peer_state_reason")
        return f"peer {p.get('queried_peer_state', '?')}" + (f" ({reason})" if reason else "")
    if tool_id == "interface":
        ifaces = p.get("interfaces", {})
        down = [n for n, i in ifaces.items() if "down" in (i.get("link_state"), i.get("admin_state"))]
        return f"down: {', '.join(down)}" if down else f"{len(ifaces)} interface(s) up"
    if tool_id == "tcp_port":
        return "port 179 reachable" if p.get("reachable") else "port 179 unreachable"
    if tool_id == "config":
        if not p.get("has_baseline"):
            return "running config read (no baseline)"
        return "drifted from baseline" if p.get("drifted") else "matches baseline"
    if tool_id == "ml_classify":
        return f"{p.get('label')} ({p.get('confidence', 0):.2f})"
    return "ok"
