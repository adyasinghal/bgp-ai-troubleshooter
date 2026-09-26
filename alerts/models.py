"""Structured alert data shared by the evaluator, API, store, and dashboard."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class AlertSeverity(str, Enum):
    """Severity assigned to an alert or recovery event."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    RECOVERY = "RECOVERY"


class AlertType(str, Enum):
    """Category that identifies the source or lifecycle of an alert."""

    BGP_STATE = "BGP_STATE"
    INTERFACE = "INTERFACE"
    TCP = "TCP"
    CONFIG = "CONFIG"
    RECOVERY = "RECOVERY"


@dataclass
class Alert:
    """Evidence-backed event shown by the API, store, and dashboard."""

    alert_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    device: str = ""
    neighbor: str = ""

    alert_type: AlertType = AlertType.BGP_STATE
    severity: AlertSeverity = AlertSeverity.WARNING

    previous_state: Optional[str] = None
    current_state: Optional[str] = None

    message: str = ""
    cause: str = ""
    recommended_action: str = ""

    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dictionary representation of the alert."""
        return {
            "alert_id": self.alert_id,
            "timestamp": self.timestamp,
            "device": self.device,
            "neighbor": self.neighbor,
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "previous_state": self.previous_state,
            "current_state": self.current_state,
            "message": self.message,
            "cause": self.cause,
            "recommended_action": self.recommended_action,
            "evidence": self.evidence,
        }

    def __repr__(self) -> str:
        return (
            f"Alert(severity={self.severity.value}, type={self.alert_type.value}, "
            f"device={self.device!r}, neighbor={self.neighbor!r}, "
            f"state={self.previous_state!r}→{self.current_state!r})"
        )
