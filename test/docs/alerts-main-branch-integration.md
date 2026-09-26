# Alert integration on the `alert` branch

## Integration status

The alert component is wired into the current analyzer reasoning loop and the
standalone live telemetry monitor. The existing analyzer `Verdict`, API tool
result, and rules response shapes are unchanged.

Every terminal `Verdict` from `analyzer.rules_engine.diagnose` is passed to
`alerts.integration.publish_verdict`, including rule-based, accepted ML, LLM,
and unresolved results. The bridge only treats a verdict as healthy when its
rules evidence confirms that the queried BGP peer is `Established`. Therefore
a resolved fault diagnosis remains an alert, while confirmed health after an
incident emits a recovery event.

Both the analyzer integration and `NetworkMonitor` pass changes through
`alerts.store.record_transition`. Under the history lock, this operation
reloads the latest state for the device/peer, suppresses an unchanged fault,
and persists either a new incident or a recovery event. This makes duplicate
suppression and recovery detection consistent when the monitor and analyzer
run in separate processes that share the same history file.

## Dashboard behavior

The Streamlit dashboard shows the latest unresolved incident per
device/peer under **Active incidents**. A recovery moves that device/peer out
of this section. Recovery events remain available separately under
**Resolved incident history**, rather than appearing as active alerts.
Dashboard timestamps are displayed in Indian Standard Time (IST).

The dashboard reads the repository-root `alerts_log.json` directly, so no
additional API route is required. Existing `/tools/*`, `/rules/*`, and
`/health` API contracts remain unchanged.

No changes to pre-existing `main`-branch API files are required for this
integration. The analyzer hook is included in this branch's
`analyzer/rules_engine.py`.

## Live verification and test commands

Run the complete test suite at the repository root:

```powershell
python -m pytest -q
```

Run the independent alert component copy:

```powershell
Set-Location test
python -m pytest -q tests/alerts
python demo_alert_flow.py
```

Run the live monitor and dashboard in separate terminals after installing
root requirements and configuring SSH credentials for the router:

```powershell
python -m alerts.monitor --host <router-management-address> --peer <bgp-peer-address> --interval 15
python -m streamlit run alerts/dashboard.py
```

The analyzer CLI uses the same tool API and automatically publishes its
terminal verdict to the alert history:

```powershell
python -m analyzer.run "BGP peer status" --host <router-management-address> --peer <bgp-peer-address>
```

Install the dashboard dependency separately if it is not part of the root
environment:

```powershell
python -m pip install -r alerts/requirements.txt
```

Do not use the lab's `admin/admin` credentials outside a disposable lab.
