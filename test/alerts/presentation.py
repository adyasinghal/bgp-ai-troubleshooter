"""Presentation helpers for alert history."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

INDIA_TIME = ZoneInfo("Asia/Kolkata")


def format_indian_time(timestamp: str) -> str:
    """Format ISO-8601 timestamps in Indian Standard Time."""
    value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(INDIA_TIME).strftime("%d %b %Y, %I:%M:%S %p IST")


def split_current_and_resolved(
    events: list[dict],
) -> tuple[list[dict], list[dict]]:
    """Return current incidents separately from their resolved-event history."""
    latest_by_session: dict[tuple[object, object], dict] = {}
    for event in events:
        latest_by_session[(event.get("device"), event.get("neighbor"))] = event

    current = [
        event
        for event in latest_by_session.values()
        if event.get("severity") != "RECOVERY"
    ]
    current.sort(key=lambda event: event.get("timestamp", ""), reverse=True)

    resolved = [event for event in events if event.get("severity") == "RECOVERY"]
    resolved.sort(key=lambda event: event.get("timestamp", ""), reverse=True)
    return current, resolved
