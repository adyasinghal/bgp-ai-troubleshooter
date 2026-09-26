# Isolated alert-branch test copy

This directory mirrors every file in the branch's `alerts/` package, its
alert-component tests, and the tool modules required by the monitor. It also
includes the integration notes. The application source outside this component
is not duplicated.

Run the copied component tests from this directory:

```powershell
python -m pytest tests/alerts -q
```

Run the synthetic monitor end-to-end demo:

```powershell
python demo_alert_flow.py
```

The demo uses fake tool results and a temporary event-history file. It does
not connect to routers or alter the repository's existing alert history.

To smoke-test the copied Streamlit dashboard:

```powershell
python -m streamlit run alerts/dashboard.py --server.headless true --server.port 8768
```

Then open `http://127.0.0.1:8768`. The demo dashboard reads the repository-root
`alerts_log.json` by default; use an existing local history only for viewing.
Analyzer verdicts are published from the analyzer's reasoning loop to the
same alert history used by the monitor and dashboard. The copied component
tests exercise the bridge and event lifecycle; live router telemetry requires
the full application source and configured SSH access.
