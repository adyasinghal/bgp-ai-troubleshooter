from pathlib import Path

import streamlit as st

from alerts.store import AlertStoreError, load_alerts

ALERTS_LOG_PATH = Path(__file__).parent.parent / "alerts_log.json"

st.set_page_config(
    page_title="Network Operations | BGP Alerts",
    page_icon="O",
    layout="wide",
    initial_sidebar_state="collapsed",
)


@st.fragment(run_every="5s")
def render_dashboard() -> None:
    st.title("Real-Time Network Analysis & Diagnostics")
    try:
        alerts = load_alerts(ALERTS_LOG_PATH)
    except AlertStoreError as error:
        st.error(str(error))
        return

    latest_by_session = {}
    for alert in alerts:
        latest_by_session[(alert.get("device"), alert.get("neighbor"))] = alert

    active_alerts = list(latest_by_session.values())
    critical_count = sum(
        1 for alert in active_alerts if alert.get("severity") == "CRITICAL"
    )
    warning_count = sum(
        1 for alert in active_alerts if alert.get("severity") == "WARNING"
    )
    recovery_count = sum(
        1 for alert in alerts if alert.get("severity") == "RECOVERY"
    )

    metrics = st.columns(4)
    metrics[0].metric("Total Events", len(alerts))
    metrics[1].metric("Active Critical Incidents", critical_count)
    metrics[2].metric("Active Warnings / Drift", warning_count)
    metrics[3].metric("Recovery Events", recovery_count)
    st.divider()

    if not alerts:
        st.info("No incidents recorded. Waiting for analyzer or monitor telemetry.")
        return

    filters = st.columns([1, 1, 2])
    severity_filter = filters[0].selectbox(
        "Severity", ["All Events", "CRITICAL", "WARNING", "RECOVERY"]
    )
    type_filter = filters[1].selectbox(
        "Incident Type",
        ["All Types"] + sorted({alert.get("alert_type", "GENERAL") for alert in alerts}),
    )
    query = filters[2].text_input(
        "Filter by host, peer, or message",
        placeholder="e.g. router1, 10.0.0.2, eth1",
    ).lower()

    filtered_alerts = alerts
    if severity_filter != "All Events":
        filtered_alerts = [
            alert for alert in filtered_alerts
            if alert.get("severity") == severity_filter
        ]
    if type_filter != "All Types":
        filtered_alerts = [
            alert for alert in filtered_alerts
            if alert.get("alert_type") == type_filter
        ]
    if query:
        fields = ("device", "neighbor", "cause", "message", "recommended_action")
        filtered_alerts = [
            alert for alert in filtered_alerts
            if any(query in str(alert.get(field, "")).lower() for field in fields)
        ]

    st.write(f"Displaying **{len(filtered_alerts)}** incident record(s):")
    for alert in reversed(filtered_alerts):
        severity = alert.get("severity", "INFO")
        device = alert.get("device", "N/A")
        neighbor = alert.get("neighbor", "N/A")
        message = alert.get("message", "")
        cause = alert.get("cause", "")
        action = alert.get("recommended_action", "")

        with st.container(border=True):
            heading, timestamp = st.columns([3, 1])
            heading.subheader(f"{device} ↔ {neighbor}")
            timestamp.caption(alert.get("timestamp", ""))

            details, payload = st.columns([3, 2])
            if severity == "RECOVERY":
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


render_dashboard()
