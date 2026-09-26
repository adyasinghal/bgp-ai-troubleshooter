"""Suppress repeated alerts and create recovery events for healthy sessions."""

from __future__ import annotations

import logging
from typing import Optional

from alerts.models import Alert, AlertSeverity, AlertType

logger = logging.getLogger(__name__)


class AlertStateTracker:
    """Track the last emitted alert for each device and neighbor pair."""

    def __init__(self) -> None:
        self._state: dict[tuple[str, str], Optional[Alert]] = {}

    def check_and_update(self, new_alert: Alert) -> Optional[Alert]:
        """
        Evaluate a freshly-generated alert against stored state.

        Returns
        -------
        Alert
            The alert to emit.  This may be the original alert (new/changed
            condition) or a RECOVERY alert (state improved to Established).
        None
            The condition is unchanged — suppress (do not re-emit).
        """
        key = (new_alert.device, new_alert.neighbor)
        last = self._state.get(key)

        if last is None:
            logger.debug("StateTracker: new entry for %s — emitting alert.", key)
            self._state[key] = new_alert
            return new_alert

        if (
            last.severity == new_alert.severity
            and last.alert_type == new_alert.alert_type
            and (
                last.current_state == new_alert.current_state
                or last.current_state is None
                or new_alert.current_state is None
            )
        ):
            logger.debug(
                "StateTracker: state unchanged for %s (%s %s) — suppressing.",
                key,
                new_alert.severity.value,
                new_alert.current_state,
            )
            if last.current_state is None and new_alert.current_state is not None:
                self._state[key] = new_alert
            return None

        logger.info(
            "StateTracker: state changed for %s: %s -> %s; emitting alert.",
            key,
            last.current_state,
            new_alert.current_state,
        )
        new_alert.previous_state = last.current_state
        self._state[key] = new_alert
        return new_alert

    def record_healthy(self, device: str, neighbor: str) -> Optional[Alert]:
        """
        Called when AlertEvaluator returns None (session is healthy).

        If the previous state was unhealthy, this generates and returns a
        RECOVERY alert.  If the previous state was already healthy (or
        unknown), returns None.

        Parameters
        ----------
        device : str
        neighbor : str

        Returns
        -------
        Alert (RECOVERY severity) or None
        """
        key = (device, neighbor)
        last = self._state.get(key)

        if last is None:
            return None

        if last.severity == AlertSeverity.RECOVERY:
            return None

        previous_state = last.current_state or "unknown"
        logger.info(
            "StateTracker: recovery for %s: %s -> Established; emitting RECOVERY.",
            key,
            previous_state,
        )
        recovery = Alert(
            device=device,
            neighbor=neighbor,
            alert_type=AlertType.RECOVERY,
            severity=AlertSeverity.RECOVERY,
            previous_state=last.current_state,
            current_state="Established",
            message=(
                f"BGP session to {neighbor} has recovered and is now ESTABLISHED."
            ),
            cause=(
                f"The condition that caused the previous {last.severity.value} alert "
                f"({last.alert_type.value}: {previous_state}) has been resolved."
            ),
            recommended_action=(
                "Verify that the BGP session remains stable by monitoring 'show bgp summary' "
                "over the next few minutes."
            ),
            evidence={"previous_alert_type": last.alert_type.value,
                      "previous_bgp_state": last.current_state},
        )
        self._state[key] = recovery
        return recovery

    def reset(self, device: str, neighbor: str) -> None:
        """
        Clear stored state for a specific (device, neighbor) pair.
        Useful in tests or after a planned maintenance window.
        """
        self._state.pop((device, neighbor), None)

    def reset_all(self) -> None:
        """Clear all stored state (e.g. on service restart)."""
        self._state.clear()

    def restore(self, alert: Alert) -> None:
        """Restore the last emitted alert for a session from durable history."""
        self._state[(alert.device, alert.neighbor)] = alert

    def current_state(self, device: str, neighbor: str) -> Optional[Alert]:
        """Return the last emitted alert for a pair, or None if no alert is stored."""
        return self._state.get((device, neighbor))
