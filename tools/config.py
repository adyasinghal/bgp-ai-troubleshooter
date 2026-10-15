"""
tool4: Config
Purpose: config diff -> pulls `show running-config` from the FRR node and
diffs it against a stored baseline (e.g. last-known-good config for that
node), so the reasoning loop can spot a drifted `router bgp` / neighbor
statement causing the stuck session.

Not every drift matters: a changed neighbor description is drift, but it can't
break a session. `severity` says which kind of change the diff holds:
    None      - no baseline, so drift is unknown
    "none"    - running config matches the baseline
    "cosmetic"- only descriptions, comments, version/banner lines changed
    "bgp"     - lines that can change BGP behaviour changed (see relevant_changes)
"""

import difflib
import re
from pathlib import Path

from tools.base_tool import BaseTool, ToolResult

BASELINE_DIR = Path(__file__).parent / "config_baselines"
BASELINE_DIR.mkdir(exist_ok=True)

# Lines whose change cannot affect forwarding or BGP
COSMETIC_RE = re.compile(
    r"^\s*(?:!.*|$|Building configuration.*|Current configuration.*|frr version .*|frr defaults .*"
    r"|end|description .*|neighbor \S+ description .*|log .*|banner .*|line vty|exec-timeout .*"
    r"|service .*)$"
)


def _changed(diff: list[str]) -> list[str]:
    return [l for l in diff if l[:1] in "+-" and not l.startswith(("+++", "---"))]


def classify(changed: list[str]) -> tuple[str, list[str]]:
    """(severity, relevant changed lines) for the +/- lines of a diff."""
    if not changed:
        return "none", []
    relevant = [l for l in changed if not COSMETIC_RE.match(l[1:])]
    return ("bgp" if relevant else "cosmetic"), relevant


class ConfigTool(BaseTool):
    tool_id = "config"

    def run(self, host: str) -> ToolResult:
        result = self.device_client.run_vtysh(host, "show running-config")
        baseline = self._load_baseline(host)
        diff_lines = list(
            difflib.unified_diff(
                baseline.splitlines(),
                result.output.splitlines(),
                fromfile="baseline",
                tofile="running",
                lineterm="",
            )
        )
        changed = _changed(diff_lines)
        severity, relevant = classify(changed) if baseline else (None, [])
        parsed = {
            "has_baseline": bool(baseline),
            "diff": diff_lines,
            # no baseline -> unknown
            "drifted": len(diff_lines) > 0 if baseline else None,
            "severity": severity,
            "relevant_changes": relevant,
        }
        return self._wrap(host, result, parsed)

    def save_baseline(self, host: str, config_text: str):
        """Call once after confirming a config is known-good."""
        (BASELINE_DIR / f"{host}.cfg").write_text(config_text)

    def _load_baseline(self, host: str) -> str:
        path = BASELINE_DIR / f"{host}.cfg"
        return path.read_text() if path.exists() else ""
