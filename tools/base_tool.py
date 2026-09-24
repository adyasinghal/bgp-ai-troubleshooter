"""
Shared base for all Tool cohort tools.

Each tool in the cohort (BGP state, Interface, TCP/port, Config) follows the
same shape: take a target host/peer, run its CLI command via the device
client, parse the raw output into a small structured result, and return it
through the REST API layer for the Reasoning loop to consume.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from tools.device_client import FRRDeviceClient, DeviceResult


@dataclass
class ToolResult:
    tool_id: str
    host: str
    command: str
    success: bool
    raw_output: str
    parsed: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "tool_id": self.tool_id,
            "host": self.host,
            "command": self.command,
            "success": self.success,
            "raw_output": self.raw_output,
            "parsed": self.parsed,
            "error": self.error,
        }


class BaseTool:
    tool_id: str = "base"

    def __init__(self, device_client: Optional[FRRDeviceClient] = None):
        self.device_client = device_client or FRRDeviceClient()

    def _wrap(self, host: str, result: DeviceResult, parsed: dict) -> ToolResult:
        return ToolResult(
            tool_id=self.tool_id,
            host=host,
            command=result.command,
            success=result.success,
            raw_output=result.output,
            parsed=parsed,
            error=result.error,
        )
