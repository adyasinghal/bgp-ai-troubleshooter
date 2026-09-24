"""Verdict: the result the Analyze stage hands to Diagnose/Alert."""
from dataclasses import dataclass, field


@dataclass
class Verdict:
    resolved: bool
    root_cause: str | None = None
    suggested_fix: str | None = None
    checked: list[str] = field(default_factory=list)    # tools tried, in order
    evidence: list[dict] = field(default_factory=list)  # each tool's output
    source: str | None = None           # who decided: "rules" | "ml" | "llm"
    confidence: str | None = None       # "0.87" for ml, "low/medium/high" for llm
    ml_prediction: dict | None = None   # ML engine's opinion, even when it didn't decide
    next_checks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # e.g. why the LLM was skipped

    def pretty(self) -> str:
        lines = [
            f"Resolved:      {self.resolved}",
            f"Root cause:    {self.root_cause}",
            f"Suggested fix: {self.suggested_fix}",
            f"Decided by:    {self.source or '-'}"
            + (f" (confidence {self.confidence})" if self.confidence else ""),
            f"Tools checked: {' -> '.join(self.checked)}",
        ]
        if self.ml_prediction and self.source != "ml":
            ml = self.ml_prediction
            lines.append(f"ML opinion:    {ml['label']} ({ml['confidence']:.2f})")
        if self.next_checks:
            lines.append("Next checks:")
            lines += [f"  - {c}" for c in self.next_checks]
        lines += [f"Note:          {n}" for n in self.notes]
        return "\n".join(lines)
