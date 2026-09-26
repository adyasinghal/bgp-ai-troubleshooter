from pathlib import Path

import streamlit as st

from alerts.presentation import format_indian_time, split_current_and_resolved
from alerts.store import AlertStoreError, load_alerts

ALERTS_LOG_PATH = Path(__file__).parent.parent / "alerts_log.json"

st.set_page_config(
    page_title="Network Operations | BGP Alerts",
    page_icon="O",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _render_event(alert: dict, resolved: bool = False) -> None:
    severity = alert.get("severity", "INFO")
    device = alert.get("device", "N/A")
    neighbor = alert.get("neighbor", "N/A")
    message = alert.get("message", "")
    cause = alert.get("cause", "")
    action = alert.get("recommended_action", "")

    with st.container(border=True):
        heading, timestamp = st.columns([3, 1])
        heading.subheader(f"{device}  <->  {neighbor}")
        raw_timestamp = str(alert.get("timestamp", ""))
        try:
            timestamp.caption(format_indian_time(raw_timestamp))
        except ValueError:
            timestamp.warning(f"Invalid event timestamp: {raw_timestamp}")

        details, payload = st.columns([3, 2])
        if resolved:
            details.success(f"**RESOLVED:** {message}")
        elif severity == "CRITICAL":
            details.error(f"**CRITICAL:** {message}")
        else:
            details.warning(f"**{severity}:** {message}")

        if cause:
            details.markdown(f"**Cause:** {cause}")
        if action:
            details.markdown(f"**Recommended action:** `{action}`")
        if alert.get("plain_english"):
            details.caption(alert["plain_english"])
        details.caption(f"Incident type: {alert.get('alert_type', 'GENERAL')}")

        evidence = alert.get("evidence", {})
        if evidence:
            with payload.expander("Telemetry and evidence"):
                st.json(evidence)


@st.fragment(run_every="5s")
def render_dashboard() -> None:
    st.title("Real-Time Network Analysis & Diagnostics")
    try:
        events = load_alerts(ALERTS_LOG_PATH)
    except AlertStoreError as error:
        st.error(str(error))
        return

    active_incidents, resolved_events = split_current_and_resolved(events)
    critical_count = sum(
        1 for event in active_incidents if event.get("severity") == "CRITICAL"
    )
    warning_count = sum(
        1 for event in active_incidents if event.get("severity") == "WARNING"
    )

    metrics = st.columns(4)
    metrics[0].metric("Total Events", len(events))
    metrics[1].metric("Active Critical Incidents", critical_count)
    metrics[2].metric("Active Warnings / Drift", warning_count)
    metrics[3].metric("Resolved Incidents", len(resolved_events))
    st.caption("All event times are shown in Indian Standard Time (IST).")
    st.divider()

    if not events:
        st.info("No incidents recorded. Waiting for analyzer or monitor telemetry.")
        return

    filters = st.columns([1, 1, 2])
    severity_filter = filters[0].selectbox(
        "Severity", ["All Events", "CRITICAL", "WARNING", "RECOVERY"]
    )
    type_filter = filters[1].selectbox(
        "Incident Type",
        ["All Types"] + sorted(
            {str(event.get("alert_type", "GENERAL")) for event in events}
        ),
    )
    query = filters[2].text_input(
        "Filter by host, peer, or message",
        placeholder="e.g. router1, 10.0.0.2, eth1",
    ).lower()

    def matches(event: dict) -> bool:
        if severity_filter != "All Events" and event.get("severity") != severity_filter:
            return False
        if type_filter != "All Types" and event.get("alert_type") != type_filter:
            return False
        fields = ("device", "neighbor", "cause", "message", "recommended_action")
        return not query or any(
            query in str(event.get(field, "")).lower() for field in fields
        )

    visible_active = [event for event in active_incidents if matches(event)]
    visible_resolved = [event for event in resolved_events if matches(event)]

    st.subheader(f"Active incidents ({len(visible_active)})")
    if visible_active:
        for event in visible_active:
            _render_event(event)
    else:
        st.success("No active incidents.")

    with st.expander(f"Resolved incident history ({len(visible_resolved)})"):
        if visible_resolved:
            for event in visible_resolved:
                _render_event(event, resolved=True)
        else:
            st.info("No resolved incidents match the current filters.")


render_dashboard()
