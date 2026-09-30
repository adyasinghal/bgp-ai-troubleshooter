"""Verdict: the result the Analyze stage hands to Diagnose/Alert."""
from dataclasses import dataclass, field


@dataclass
class Verdict:
    resolved: bool
    root_cause: str | None = None
    suggested_fix: str | None = None
    checked: list[str] = field(default_factory=list)    # tools tried, in order
    evidence: list[dict] = field(default_factory=list)  # each tool's output
    source: str | None = None           # who decided: "rules" | "ml" | "llm" | "agent"
    confidence: str | None = None       # "0.87" for ml, "low/medium/high" for llm/agent
    ml_prediction: dict | None = None   # ML engine's opinion, even when it didn't decide
    next_checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # e.g. why the LLM was skipped
    # agent mode
    fault_class: str | None = None
    rules_agree: bool | None = None     # None when no rule decided
    triage: dict | None = None
    steps: list[dict] = field(default_factory=list)

    def pretty(self) -> str:
        lines = [
            f"Resolved:      {self.resolved}",
            f"Root cause:    {self.root_cause}",
            f"Suggested fix: {self.suggested_fix}",
            f"Decided by:    {self.source or '-'}"
            + (f" (confidence {self.confidence})" if self.confidence else ""),
            f"Tools checked: {' -> '.join(self.checked)}",
        ]
        if self.triage and self.triage.get("method") == "llm":
            suspects = ", ".join(h["fault_class"] for h in self.triage["hypotheses"]) or "-"
            lines.append(f"Triage:        {self.triage['symptom']} (suspects: {suspects})")
        if self.fault_class:
            lines.append(f"Fault class:   {self.fault_class}")
        if self.rules_agree is not None:
            lines.append(f"Rules agree:   {'yes' if self.rules_agree else 'NO, check the steps below'}")
        if self.ml_prediction and self.source != "ml":
            ml = self.ml_prediction
            lines.append(f"ML opinion:    {ml['label']} ({ml['confidence']:.2f})")
        if self.steps:
            lines.append("Steps:")
            for s in self.steps:
                if s.get("rejected"):
                    lines.append(f"  {s['n']}. {s.get('tool') or '-'}: rejected ({s['rejected']})")
                else:
                    lines.append(f"  {s['n']}. {s['tool']} -> {s['summary']}")
                if s.get("thought"):
                    lines.append(f"     why: {s['thought']}")
        if self.next_checks:
            lines.append("Next checks:")
            lines += [f"  - {c}" for c in self.next_checks]
        lines += [f"Note:          {n}" for n in self.notes]
        return "\n".join(lines)
