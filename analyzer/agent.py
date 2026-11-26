"""LLM agent: the LLM picks each tool, reads the result and rule finding,
and decides what to check next or concludes. Falls back to the rule chain
if the LLM is unavailable.
"""
import json
import logging

from analyzer import llm_client, ml_engine, prompts, rules_engine
from analyzer.llm_client import LLMUnavailable
from analyzer.tool_registry import ToolCallError, ToolRegistry
from analyzer.triage import llm_triage, route_context
from analyzer.verdict import Verdict

DEFAULT_MAX_STEPS = 6
RAW_OUTPUT_LIMIT = 2500
FIRST_TOOL = "bgp_state"
MAX_REJECTS = 2   # in a row, then the LLM must conclude

ROUTE_CLASSES = {"route_not_selected", "route_filtered", "route_not_received", "route_as_intended"}

# config_drift is generic, so the specific config faults count as agreeing;
# a healthy session is consistent with any route answer
AGREES_WITH = {
    "config_drift": {"config_drift", "remote_as_mismatch", "neighbor_shutdown", "neighbor_missing",
                     "auth_mismatch", "max_prefix_exceeded", "capability_mismatch", "router_id_conflict",
                     "afi_not_negotiated"},
    "healthy": {"healthy"} | ROUTE_CLASSES,
    "no_route_to_peer": {"no_route_to_peer", "interface_down", "tcp_unreachable"},
    "peer_not_listening": {"peer_not_listening", "tcp_unreachable"},
    "tcp_unreachable": {"tcp_unreachable", "peer_not_listening", "no_route_to_peer"},
}

log = logging.getLogger(__name__)


def investigate(client, question: str, host: str, peer: str | None,
                max_steps: int = DEFAULT_MAX_STEPS, use_ml: bool = True,
                trust_rules: bool = False,
                engineer_feedback: list[dict] | None = None,
                carried_steps: list[dict] | None = None) -> Verdict:
    """engineer_feedback: earlier rounds an engineer reviewed, each with what the agent
    concluded and the engineer's reply (see analyzer/review.py). The agent re-investigates
    with them in its case, so it follows the engineer's direction but still checks it.

    carried_steps: tool results from the earlier round(s). They start this investigation
    as steps already done (marked carried_over_from_round), so the agent picks up where
    it left off instead of starting over, and they don't use up its step budget."""
    registry = ToolRegistry.load(client)
    if not use_ml:
        registry.remove("ml_classify")
    ctx = {"host": host, "peer": peer, "question": question, **route_context(question)}
    run = Investigation(client, registry, question, ctx, max_steps, use_ml, trust_rules,
                        engineer_feedback or [], carried_steps or [])
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
                 max_steps: int, use_ml: bool, trust_rules: bool,
                 engineer_feedback: list[dict] | None = None,
                 carried_steps: list[dict] | None = None):
        self.client = client
        self.registry = registry
        self.question = question
        self.ctx = ctx
        self.max_steps = max_steps
        self.use_ml = use_ml
        self.trust_rules = trust_rules
        self.engineer_feedback = engineer_feedback or []
        self.model = None
        self.triage: dict | None = None
        self.steps: list[dict] = []
        self.evidence: list[dict] = []
        self.checked: list[str] = []
        self.findings: list[dict] = []
        self.calls: dict[tuple, int] = {}
        self._seed(carried_steps or [])

    def _seed(self, carried: list[dict]):
        """Start from an earlier round's tool results, as steps already done."""
        for c in carried:
            tool_id = c.get("tool")
            if self.registry.tools.get(tool_id, {}).get("kind") != "device" or not c.get("result"):
                continue
            r = c["result"]
            result = {"success": r.get("success"), "error": r.get("error"),
                      "parsed": r.get("parsed"), "raw_output": r.get("raw_output")}
            finding = c.get("rule_finding") or rules_engine.evaluate(tool_id, result, self.ctx)
            summary = c.get("summary") or summarize(tool_id, result)
            self.checked.append(tool_id)
            self.findings.append(finding)
            self.evidence.append({"tool": tool_id, **result})
            self.steps.append({"n": len(self.steps) + 1, "thought": c.get("thought"),
                               "intent": c.get("intent"), "tool": tool_id, "args": c.get("args") or {},
                               "summary": summary, "result": result, "rule_finding": finding,
                               "carried_over_from_round": c.get("round")})
        if self.steps:
            log.info("Carried over %d step(s) from earlier rounds: %s", len(self.steps),
                     ", ".join(s["tool"] for s in self.steps))

    def run(self) -> Verdict:
        log.info("Agent start: host=%s peer=%s max_steps=%d",
                 self.ctx["host"], self.ctx["peer"], self.max_steps)
        triage_question = self.question
        if self.engineer_feedback:   # the engineer's reply can change what to suspect first
            triage_question += "\n\nEngineer's reply to the last diagnosis: " + \
                self.engineer_feedback[-1]["engineer_says"]
            log.info("Re-investigating with engineer feedback (round %d): %s",
                     len(self.engineer_feedback) + 1, self.engineer_feedback[-1]["engineer_says"])
        self.triage = llm_triage(triage_question, self.ctx["host"], self.ctx["peer"], self.registry)
        if not self.triage["in_scope"] and not self.engineer_feedback:
            return Verdict(False, "This doesn't look like a question about this router's BGP sessions.",
                           "Ask about a BGP session, its peer, or the links and config behind it.",
                           source="agent", triage=self.triage)
        offset = len(self.steps)          # carried-over steps don't use up the budget
        for i in range(self.max_steps):
            n = offset + i + 1
            decision = self._decide(steps_left=self.max_steps - i)
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
            recent = self.steps[-MAX_REJECTS:]
            if self.evidence and len(recent) == MAX_REJECTS and all("rejected" in r for r in recent):
                log.info("%d rejected steps in a row; asking for a conclusion", MAX_REJECTS)
                break
        return self._verdict(self._decide(steps_left=0))

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
        case = {
            "question": self.question,
            "host": self.ctx["host"],
            "peer": self.ctx["peer"],
            "triage": self.triage,
            "fault_classes": prompts.FAULT_CLASSES,
            "tools": self.registry.for_prompt(),
            "steps": [_step_for_prompt(s) for s in self.steps],
        }
        if self.engineer_feedback:
            case["engineer_feedback"] = self.engineer_feedback
        return case

    def _call(self, n: int, decision: dict) -> dict | None:
        tool_id = decision.get("tool_id") or ""
        try:
            args = self.registry.validate(tool_id, decision.get("args"))
            if "prefix" in self.registry.tools[tool_id]["args_schema"] and self.ctx.get("prefix"):
                args.setdefault("prefix", self.ctx["prefix"])   # as the rule chain does
            key = (tool_id, json.dumps(args, sort_keys=True))
            if self.registry.tools[tool_id]["kind"] == "internal":
                key += (len(self._device_evidence()),)
            if key in self.calls:
                untried = [t for t in self.registry.tools if t not in self.checked
                           and self.registry.tools[t]["kind"] == "device"]
                raise ToolCallError(f"{tool_id} was already called with these args in step "
                                    f"{self.calls[key]}; use that result. Tools not called yet: "
                                    f"{', '.join(untried) or 'none'}")
            if (tool_id != FIRST_TOOL and FIRST_TOOL in self.registry.tools
                    and FIRST_TOOL not in self.checked):
                if self.engineer_feedback:
                    # In a re-investigation the model may think the first check is already done.
                    # Run it now instead of rejecting the step (a small model can keep retrying
                    # the same rejected call until the step budget runs out).
                    log.info("Step %d: %s asked before %s; running %s first", n, tool_id,
                             FIRST_TOOL, FIRST_TOOL)
                    return self._call(n, {**decision, "tool_id": FIRST_TOOL, "args": {},
                                          "thought": f"[{FIRST_TOOL} runs first; {tool_id} was asked for] "
                                                     + (decision.get("thought") or "")})
                raise ToolCallError(f"call {FIRST_TOOL} first")
        except ToolCallError as e:
            self._reject(n, decision, str(e))
            return None

        self.calls[key] = n
        result = self.registry.execute(self.client, tool_id, args, self.ctx, self.evidence)
        finding = rules_engine.evaluate(tool_id, result, self.ctx)
        if finding["decision"] == "continue" and "hint" not in finding:
            finding["suggested_next_intent"] = self.registry.next_intent_after(tool_id)
        if finding.get("suggested_next_intent"):
            # bgp_state points at bgp_state_<state>, whose only tool is bgp_state again: the model
            # followed it, got rejected as a duplicate and was forced to conclude. Skip to the first
            # intent with a tool that hasn't run yet.
            finding["suggested_next_intent"] = self._next_new_intent(finding["suggested_next_intent"],
                                                                     self.checked + [tool_id])
        if finding["decision"] == "healthy":
            finding["note"] = ("The session is up. If the question is about the session, conclude; "
                               "if it is about routes or prefixes, continue with route_table.")
        elif finding["decision"] == "root_cause":
            finding["note"] = "The rule book considers this decided. Conclude unless other evidence contradicts it."
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

    def _next_new_intent(self, intent: str | None, ran: list[str]) -> str | None:
        seen = set()
        while intent and intent not in seen:
            seen.add(intent)
            rules = [r for r in self.registry.rules if r["intent"] == intent]
            if not rules or any(r["tool_id"] not in ran for r in rules):
                return intent
            intent = min(rules, key=lambda r: r["priority"]).get("next_intent_on_fail")
        return intent

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

    def _verdict(self, decision: dict) -> Verdict:
        missing = _missing_diagnosis_fields(decision)
        if missing:
            raise LLMUnavailable(f"the model's conclusion is missing {', '.join(missing)}")
        d = decision["diagnosis"]
        notes = [f"Diagnosed by {self.model}"]
        if self.engineer_feedback:
            notes.append(f"Re-investigated with engineer feedback ({len(self.engineer_feedback)} round(s))")
        resolved = bool(d["resolved"])
        if not self.evidence:
            resolved = False
            notes.append("No tool ran, so the conclusion has no evidence")

        decided = [f for f in self.findings if f["decision"] != "continue"]
        rules_agree = (any(d["fault_class"] in AGREES_WITH.get(f["fault_class"], {f["fault_class"]})
                           for f in decided) if decided else None)
        if rules_agree is False:
            log.warning("Agent's fault class %s disagrees with the rule findings %s",
                        d["fault_class"], [f["fault_class"] for f in decided])
        log.info("Agent concluded: resolved=%s fault_class=%s confidence=%s rules_agree=%s root cause: %s",
                 resolved, d["fault_class"], d["confidence"], rules_agree, d["root_cause"])

        return Verdict(resolved, d["root_cause"], d["suggested_fix"], self.checked, self.evidence,
                       source="agent", confidence=d["confidence"],
                       ml_prediction=None if d["fault_class"] in ROUTE_CLASSES else self._ml_opinion(),
                       next_checks=list(d["next_checks"]), notes=notes,
                       fault_class=d["fault_class"], rules_agree=rules_agree,
                       triage=self.triage, steps=self._public_steps(), work=self._work())

    def _rules_verdict(self, finding: dict) -> Verdict:
        log.info("Rule finding %s after %s; stopping (--trust-rules)", finding["decision"], self.checked[-1])
        return Verdict(True, finding["cause"], finding["fix"], self.checked, self.evidence,
                       source="rules", ml_prediction=self._ml_opinion(),
                       notes=["Stopped at the first rule finding (--trust-rules)"],
                       fault_class=finding["fault_class"], triage=self.triage,
                       steps=self._public_steps(), work=self._work())

    def _device_evidence(self) -> list[dict]:
        return [e for e in self.evidence
                if self.registry.tools.get(e["tool"], {}).get("kind") == "device"]

    def _ml_opinion(self) -> dict | None:
        device = self._device_evidence()
        if not (self.use_ml and device):
            return None
        ml = ml_engine.predict(device, self.ctx["peer"])
        log.info("ML second opinion: %s (%.2f)", ml.label, ml.confidence)
        return ml.to_dict()

    def _public_steps(self) -> list[dict]:
        keys = ("n", "thought", "intent", "tool", "args", "summary", "rejected", "carried_over_from_round")
        return [{k: s[k] for k in keys if k in s} for s in self.steps]

    def _work(self) -> list[dict]:
        """Every tool step with its full result, for the next round to carry over."""
        return [{"tool": s["tool"], "args": s["args"], "thought": s.get("thought"),
                 "intent": s.get("intent"), "summary": s["summary"], "result": s["result"],
                 "rule_finding": s["rule_finding"], "round": s.get("carried_over_from_round")}
                for s in self.steps if "result" in s]


def _missing_diagnosis_fields(decision: dict) -> list[str]:
    d = decision.get("diagnosis")
    required = prompts.diagnosis_schema()["required"]
    if not isinstance(d, dict):
        return ["diagnosis"]
    return [k for k in required if k not in d]


def _step_for_prompt(step: dict) -> dict:
    if "rejected" in step:
        return {"n": step["n"], "tool": step["tool"], "args": step["args"], "rejected": step["rejected"]}
    r = step["result"]
    parsed = r.get("parsed") or {}
    if step["tool"] == "config" and not parsed.get("has_baseline"):
        parsed = {**parsed, "diff": "(no baseline; see raw_output)"}
    shown = {"success": r.get("success"), "error": r.get("error"), "parsed": parsed}
    if r.get("raw_output"):
        shown["raw_output"] = _trim(r["raw_output"])
    out = {"n": step["n"], "thought": step["thought"], "intent": step["intent"], "tool": step["tool"],
           "args": step["args"], "result": shown, "rule_finding": step["rule_finding"]}
    if step.get("carried_over_from_round"):
        out["carried_over_from_round"] = step["carried_over_from_round"]
    return out


def _trim(text: str) -> str:
    limit = RAW_OUTPUT_LIMIT
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters cut]"


def summarize(tool_id: str, result: dict) -> str:
    if not result.get("success", True):
        return f"failed: {result.get('error')}"
    p = result.get("parsed") or {}
    if tool_id == "bgp_state":
        reason = p.get("queried_peer_state_reason")
        return f"peer {p.get('queried_peer_state', '?')}" + (f" ({reason})" if reason else "")
    if tool_id == "bgp_neighbor":
        if not p.get("configured", True):
            return "not configured"
        note = p.get("notification")
        return f"{p.get('state')}, last reset: " + (
            f"notification {note['direction']} ({note['error']})" if note else (p.get("last_reset") or "never"))
    if tool_id == "interface":
        ifaces = p.get("interfaces", {})
        down = [n for n, i in ifaces.items() if "down" in (i.get("link_state"), i.get("admin_state"))]
        return f"down: {', '.join(down)}" if down else f"{len(ifaces)} interface(s) up"
    if tool_id == "tcp_port":
        if p.get("reachable"):
            return "port 179 reachable"
        return "port 179 unreachable" + (f" ({p['reason']})" if p.get("reason") else "")
    if tool_id == "config":
        if not p.get("has_baseline"):
            return "running config read (no baseline)"
        if not p.get("drifted"):
            return "matches baseline"
        return "drifted from baseline" + (f" ({p['severity']})" if p.get("severity") else "")
    if tool_id == "route_to_peer":
        if not p.get("has_route"):
            return "no route to the peer"
        if p.get("is_default"):
            return "only the default route matches the peer"
        return f"route {p.get('prefix')} via {p.get('protocol')} on {p.get('interface') or p.get('next_hop')}"
    if tool_id == "prefix_list":
        ev = p.get("evaluation") or {}
        denied = [n for n, e in ev.items() if e.get("action") == "deny"]
        return f"{p.get('count', 0)} list(s)" + (f", denying {p.get('prefix')}: {', '.join(denied)}" if denied else "")
    if tool_id == "route_table":
        if p.get("in_table") is False:
            return f"{p.get('prefix')} not in the BGP table"
        best = p.get("best_path")
        if not best:
            return f"{p.get('path_count', 0)} path(s), none best"
        reason = f" ({best['best_reason']})" if best.get("best_reason") else ""
        return f"{p['path_count']} path(s), best via {best['next_hop']}{reason}"
    if tool_id == "route_map":
        denies = [f"{m['name']} {m['sequence']}" for m in p.get("route_maps", [])
                  if m["action"] == "deny" and m.get("invoked")]
        return f"{p.get('count', 0)} entries" + (f", denying: {', '.join(denies)}" if denies else "")
    if tool_id == "ml_classify":
        return f"{p.get('label')} ({p.get('confidence', 0):.2f})"
    return "ok"
