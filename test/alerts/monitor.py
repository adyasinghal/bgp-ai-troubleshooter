"""Poll live devices, evaluate telemetry, and persist alert transitions."""

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from tools.base_tool import ToolResult
from tools.bgp_state import BGPStateTool
from tools.interface import InterfaceTool
from tools.tcp_port import TCPPortTool
from tools.config import ConfigTool

from alerts.models import Alert
from alerts.evaluator import AlertEvaluator
from alerts.state_tracker import AlertStateTracker
from alerts.store import (
    ALERTS_LOG_PATH,
    AlertStoreError,
    append_alert,
    restore_tracker,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("BGP-Monitor")


class NetworkMonitor:
    def __init__(self, interval: int = 15, log_path: Path = ALERTS_LOG_PATH):
        self.interval = interval
        self.log_path = log_path
        self.evaluator = AlertEvaluator()
        self.tracker = AlertStateTracker()

        self.bgp_tool = BGPStateTool()
        self.interface_tool = InterfaceTool()
        self.tcp_tool = TCPPortTool()
        self.config_tool = ConfigTool()
        self._restore_tracker()

    def _restore_tracker(self) -> None:
        try:
            restore_tracker(self.tracker, self.log_path)
        except AlertStoreError:
            logger.exception("Could not restore alert tracker from %s", self.log_path)
            raise

    def record_alert(self, alert: Alert) -> None:
        append_alert(alert, log_path=self.log_path)
        logger.info(
            "Emitted Alert: [%s] %s on %s",
            alert.severity.value,
            alert.alert_type.value,
            alert.device,
        )

    def poll_device(self, host: str, peer: Optional[str] = None) -> None:
        logger.info(f"Polling telemetry from {host} (peer: {peer or 'all'})...")
        tool_results = [
            self._run_tool(self.bgp_tool, host, peer),
            self._run_tool(self.interface_tool, host),
        ]
        if peer:
            tool_results.append(self._run_tool(self.tcp_tool, host, peer, 179))
        tool_results.append(self._run_tool(self.config_tool, host))

        target_peer = peer or "unknown"
        alert = self.evaluator.evaluate(tool_results, host, target_peer)

        if alert is None:
            recovery = self.tracker.record_healthy(host, target_peer)
            if recovery:
                self.record_alert(recovery)
        else:
            final_alert = self.tracker.check_and_update(alert)
            if final_alert:
                self.record_alert(final_alert)
            else:
                logger.info(
                    "Duplicate alert suppressed for %s/%s (%s)",
                    host,
                    target_peer,
                    alert.alert_type.value,
                )

    @staticmethod
    def _run_tool(tool, host: str, *args) -> ToolResult:
        try:
            return tool.run(host, *args)
        except Exception as error:
            logger.exception("Tool %s raised an error on %s", tool.tool_id, host)
            return ToolResult(
                tool_id=tool.tool_id,
                host=host,
                command="",
                success=False,
                raw_output="",
                error=f"{type(error).__name__}: {error}",
            )

    def run_loop(
        self,
        targets: list[tuple[str, str]],
        max_cycles: Optional[int] = None,
    ) -> None:
        logger.info(f"Starting Network Monitor loop (Interval: {self.interval}s, Targets: {targets})")
        cycle = 0
        while True:
            cycle += 1
            logger.info(f"--- Telemetry Polling Cycle {cycle} ---")
            for host, peer in targets:
                self.poll_device(host, peer)

            if max_cycles and cycle >= max_cycles:
                logger.info("Reached maximum requested polling cycles. Stopping.")
                break

            time.sleep(self.interval)


def main():
    parser = argparse.ArgumentParser(description="Real-Time Network Telemetry Monitor")
    parser.add_argument("--interval", type=int, default=15, help="Polling interval in seconds")
    parser.add_argument("--cycles", type=int, default=None, help="Max polling cycles (default: infinite)")
    parser.add_argument("--host", required=True, help="Target router management IP or hostname")
    parser.add_argument("--peer", required=True, help="Target BGP peer IP")
    args = parser.parse_args()

    monitor = NetworkMonitor(interval=args.interval)
    monitor.run_loop([(args.host, args.peer)], max_cycles=args.cycles)


if __name__ == "__main__":
    main()
