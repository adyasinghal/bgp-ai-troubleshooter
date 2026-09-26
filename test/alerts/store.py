"""Persist alert history shared by the monitor, API, and dashboard."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from alerts.formatter import format_plain_english
from alerts.models import Alert, AlertSeverity, AlertType
from alerts.state_tracker import AlertStateTracker

ALERTS_LOG_PATH = Path(__file__).parent.parent / "alerts_log.json"
logger = logging.getLogger(__name__)


class AlertStoreError(RuntimeError):
    pass


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def load_alerts(log_path: Path = ALERTS_LOG_PATH) -> list[dict]:
    if not log_path.exists():
        return []
    try:
        data = json.loads(log_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AlertStoreError(f"Could not read alert history at {log_path}: {error}") from error
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise AlertStoreError(f"Alert history at {log_path} must be a JSON array of objects")
    return data


def append_alert(alert: Alert, log_path: Path = ALERTS_LOG_PATH) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(log_path):
        history = load_alerts(log_path)
        record = alert.to_dict()
        record["plain_english"] = format_plain_english(alert)
        history.append(record)

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=log_path.parent,
                prefix=log_path.name + ".",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)
                json.dump(history, temporary_file, indent=2)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, log_path)
        except OSError as error:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass
            raise AlertStoreError(f"Could not save alert history at {log_path}: {error}") from error


def restore_tracker(
    tracker: AlertStateTracker, log_path: Path = ALERTS_LOG_PATH
) -> None:
    latest: dict[tuple[str, str], dict] = {}
    for record in load_alerts(log_path):
        device = record.get("device")
        neighbor = record.get("neighbor")
        if isinstance(device, str) and isinstance(neighbor, str) and device and neighbor:
            latest[(device, neighbor)] = record

    for record in latest.values():
        try:
            tracker.restore(
                Alert(
                    alert_id=record.get("alert_id", ""),
                    timestamp=record.get("timestamp", ""),
                    device=record["device"],
                    neighbor=record["neighbor"],
                    alert_type=AlertType(record.get("alert_type", AlertType.BGP_STATE.value)),
                    severity=AlertSeverity(record.get("severity", AlertSeverity.WARNING.value)),
                    previous_state=record.get("previous_state"),
                    current_state=record.get("current_state"),
                    message=record.get("message", ""),
                    cause=record.get("cause", ""),
                    recommended_action=record.get("recommended_action", ""),
                    evidence=record.get("evidence") or {},
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping invalid alert history record: %r", record)
