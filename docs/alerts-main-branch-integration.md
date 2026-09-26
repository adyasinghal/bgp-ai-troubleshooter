# Alert component: main-branch integration handoff

## Compatibility check

This review is against the latest available `main` commit:
`1f4b58c2c144c8cfeac0a4fe5ef2e6ea24afc3be` (2026-09-24).

The repository also has a remote branch named `alert` (singular). It currently
points to that same commit as `main`, and its root contains no `alert/` or
`alerts/` directory. The alert implementation discussed here is the local
`alerts` branch contribution, not code currently present on the remote `alert`
branch.

The alert package is compatible with the current tool and analyzer data shapes:

- `tools/base_tool.py` defines `ToolResult` with `tool_id`, `host`, `command`,
  `success`, `raw_output`, `parsed`, and `error`. `AlertEvaluator` consumes
  this model and the same `parsed` keys used by the tools.
- `analyzer/verdict.py` defines the `Verdict` fields consumed by
  `alerts.bridge.verdict_to_alert`.
- `analyzer/rules_engine.py` records evidence entries with `tool`, `success`,
  `error`, and `parsed`, matching the bridge's analyzer evidence input.
- The `resolved` field means a diagnosis was reached, not necessarily that the
  network is healthy. The bridge only treats a rules verdict as healthy when
  its BGP evidence shows the peer is `Established`; a resolved fault diagnosis
  remains an alert.

Compatibility does not yet mean end-to-end integration. Current `main` does
not import the alert package, persist analyzer verdicts as alert events, or
provide an alert-history endpoint. The `api/main.py` service exposes tool
routes and health/rules endpoints only. The standalone monitor and dashboard
can run separately, but analyzer results will not appear there until the
application is wired as described below.

## Changes needed in main

These are proposed changes for the maintainers of `main`; they are not included
in the alert-component branch.

### 1. `analyzer/rules_engine.py` — record every completed verdict

Add a small integration helper that passes the final `Verdict` through
`verdict_to_alert(verdict, host, peer)`. Call it before returning from every
terminal branch of `diagnose`:

- rule result (`source="rules"`), including the healthy Established result;
- accepted ML result (`source="ml"`);
- final LLM or unresolved result.

For a fault alert, pass the alert through one process-wide
`AlertStateTracker.check_and_update` and persist a non-`None` result with
`append_alert`. When the bridge returns `None` for a healthy verdict, call
`record_healthy(host, peer)` and persist its recovery event, if any. Initialize
one tracker for the process and restore it once at startup with
`restore_tracker`; do not create a new tracker for each diagnosis.

This hook is required for analyzer-detected problems and analyzer-confirmed
recovery to enter the alert history.

### 2. `api/main.py` — expose alert history if clients need it

The existing file is the tool-cohort API, not an analyzer orchestration API.
If the dashboard or other clients need to retrieve events over HTTP, add an
alert-history route that calls `load_alerts` and returns the records. Keep the
existing `/tools/*`, `/rules/*`, and `/health` response contracts unchanged.
The standalone Streamlit dashboard can instead continue reading the shared
JSON history directly, so this endpoint is optional for that mode.

The monitor/API processes must use the same configured history path. For a
multi-host or multi-worker deployment, use a shared durable store and
coordinate tracker state; a local JSON file and separate in-memory trackers
are only suitable when processes share that file and ownership is coordinated.

### 3. `.gitignore` — exclude runtime state

Add `alerts_log.json` and `alerts_log.json.lock` so local incident history and
the lock file are not accidentally committed.

### 4. `requirements.txt` — only if adopting the dashboard in the application

The existing root dependency list does not include Streamlit. The alert
component keeps Streamlit optional in `alerts/requirements.txt`; add
`streamlit>=1.37` to root requirements only if the main application intends
to install/run this dashboard as part of its standard setup.

### 5. Tests — protect the wiring

Add tests for the analyzer verdict hook covering:

- a rules verdict for a fault is persisted;
- a healthy Established verdict is not stored as a fault;
- an unhealthy incident followed by a healthy verdict emits a recovery event;
- duplicate unchanged incidents are suppressed.

If an HTTP alert-history route is added, test its empty, populated, and store
error responses. Existing tool-result serialization should remain unchanged.

## Scope and current behavior

No main-branch application/API files or root dependencies were changed in the
alert-component branch. The standalone package has focused tests for tool
result compatibility, telemetry failures, TCP/interface faults, baseline
handling, analyzer verdict conversion, persistence/restoration, recovery, and
tool exceptions. Run them from the repository root with:

```powershell
python -m pytest tests/alerts
```

The optional standalone dashboard is started with:

```powershell
python -m pip install -r alerts/requirements.txt
python -m streamlit run alerts/dashboard.py
```
