"""
tool4: Config
Purpose: config diff -> pulls `show running-config` from the FRR node and
diffs it against a stored baseline (e.g. last-known-good config for that
node), so the reasoning loop can spot a drifted `router bgp` / neighbor
statement causing the stuck session.
"""

import difflib
from pathlib import Path

from typing import Optional

from tools.base_tool import BaseTool, ToolResult

BASELINE_DIR = Path(__file__).parent / "config_baselines"
BASELINE_DIR.mkdir(exist_ok=True)


class ConfigTool(BaseTool):
    tool_id = "config"

    def run(self, host: str) -> ToolResult:
        result = self.device_client.run_vtysh(host, "show running-config")
        baseline = self._load_baseline(host)
        parsed = self._parse(result.output, baseline, success=result.success)
        return self._wrap(host, result, parsed)

    def _parse(self, output: str, baseline: Optional[str], success: bool = True) -> dict:
        has_baseline = baseline is not None
        if not success or not has_baseline or baseline is None:
            return {
                "has_baseline": has_baseline,
                "diff": [],
                "drifted": False,
            }

        diff_lines = list(
            difflib.unified_diff(
                baseline.splitlines(),
                output.splitlines(),
                fromfile="baseline",
                tofile="running",
                lineterm="",
            )
        )
        return {
            "has_baseline": True,
            "diff": diff_lines,
            "drifted": len(diff_lines) > 0,
        }

    def save_baseline(self, host: str, config_text: str):
        """Call once after confirming a config is known-good."""
        (BASELINE_DIR / f"{host}.cfg").write_text(config_text)

    def _load_baseline(self, host: str) -> Optional[str]:
        path = BASELINE_DIR / f"{host}.cfg"
        return path.read_text() if path.exists() else None
