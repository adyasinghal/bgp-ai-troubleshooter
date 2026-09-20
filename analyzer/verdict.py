"""Verdict: the result the Analyze stage hands to Diagnose/Alert."""
from dataclasses import dataclass, field


@dataclass
class Verdict:
    resolved: bool
    root_cause: str | None = None
    suggested_fix: str | None = None
    checked: list[str] = field(default_factory=list)    # tools tried, in order
    evidence: list[dict] = field(default_factory=list)  # each tool's parsed output

    def pretty(self) -> str:
        return "\n".join([
            f"Resolved:      {self.resolved}",
            f"Root cause:    {self.root_cause}",
            f"Suggested fix: {self.suggested_fix}",
            f"Tools checked: {' -> '.join(self.checked)}",
        ])