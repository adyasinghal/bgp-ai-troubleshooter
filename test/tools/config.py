"""
tool4: Config
Purpose: config diff -> pulls `show running-config` from the FRR node and
diffs it against a stored baseline (e.g. last-known-good config for that
node), so the reasoning loop can spot a drifted `router bgp` / neighbor
statement causing the stuck session.
"""

import difflib
from pathlib import Path

from tools.base_tool import BaseTool, ToolResult

BASELINE_DIR = Path(__file__).parent / "config_baselines"
BASELINE_DIR.mkdir(exist_ok=True)


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
        parsed = {
            "has_baseline": bool(baseline),
            "diff": diff_lines,
            "drifted": len(diff_lines) > 0,
        }
        return self._wrap(host, result, parsed)

    def save_baseline(self, host: str, config_text: str):
        """Call once after confirming a config is known-good."""
        (BASELINE_DIR / f"{host}.cfg").write_text(config_text)

    def _load_baseline(self, host: str) -> str:
        path = BASELINE_DIR / f"{host}.cfg"
        return path.read_text() if path.exists() else ""
