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
        _append_alert_locked(alert, history, log_path)


def _append_alert_locked(alert: Alert, history: list[dict], log_path: Path) -> None:
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


def record_transition(
    tracker: AlertStateTracker,
    device: str,
    neighbor: str,
    alert: Alert | None,
    log_path: Path = ALERTS_LOG_PATH,
) -> Alert | None:
    """Apply and persist one alert or healthy transition under the history lock."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(log_path):
        history = load_alerts(log_path)
        latest = next(
            (
                record
                for record in reversed(history)
                if record.get("device") == device and record.get("neighbor") == neighbor
            ),
            None,
        )

        tracker.reset(device, neighbor)
        if latest is not None:
            restored = _alert_from_record(latest)
            if restored is not None:
                tracker.restore(restored)

        emitted = (
            tracker.record_healthy(device, neighbor)
            if alert is None
            else tracker.check_and_update(alert)
        )
        if emitted is not None:
            _append_alert_locked(emitted, history, log_path)
        return emitted


def _alert_from_record(record: dict) -> Alert | None:
    try:
        device = record["device"]
        neighbor = record["neighbor"]
        if not isinstance(device, str) or not isinstance(neighbor, str):
            raise ValueError("device and neighbor must be strings")
        return Alert(
            alert_id=record.get("alert_id", ""),
            timestamp=record.get("timestamp", ""),
            device=device,
            neighbor=neighbor,
            alert_type=AlertType(record.get("alert_type", AlertType.BGP_STATE.value)),
            severity=AlertSeverity(record.get("severity", AlertSeverity.WARNING.value)),
            previous_state=record.get("previous_state"),
            current_state=record.get("current_state"),
            message=record.get("message", ""),
            cause=record.get("cause", ""),
            recommended_action=record.get("recommended_action", ""),
            evidence=record.get("evidence") or {},
        )
    except (KeyError, TypeError, ValueError):
        logger.warning("Skipping invalid alert history record: %r", record)
        return None


def restore_tracker(
    tracker: AlertStateTracker, log_path: Path = ALERTS_LOG_PATH
) -> None:
    tracker.reset_all()
    latest: dict[tuple[str, str], dict] = {}
    for record in load_alerts(log_path):
        device = record.get("device")
        neighbor = record.get("neighbor")
        if isinstance(device, str) and isinstance(neighbor, str) and device and neighbor:
            latest[(device, neighbor)] = record

    for record in latest.values():
        alert = _alert_from_record(record)
        if alert is not None:
            tracker.restore(alert)
