"""Publish analyzer verdicts through the shared alert lifecycle."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from alerts.bridge import verdict_to_alert
from alerts.models import Alert
from alerts.state_tracker import AlertStateTracker
from alerts.store import ALERTS_LOG_PATH, record_transition

logger = logging.getLogger(__name__)
_tracker = AlertStateTracker()
_publish_lock = threading.RLock()


def publish_verdict(
    verdict: object,
    device: str,
    neighbor: str,
    log_path: Path = ALERTS_LOG_PATH,
) -> Alert | None:
    """Persist analyzer faults and recoveries using the same durable deduplication as polling."""
    alert = verdict_to_alert(verdict, device, neighbor)
    with _publish_lock:
        emitted = record_transition(
            _tracker,
            device=device,
            neighbor=neighbor,
            alert=alert,
            log_path=log_path,
        )
    if emitted is not None:
        logger.info(
            "Analyzer emitted alert: [%s] %s on %s",
            emitted.severity.value,
            emitted.alert_type.value,
            emitted.device,
        )
    return emitted
