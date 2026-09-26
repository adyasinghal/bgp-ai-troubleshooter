# Alerting component

This package evaluates router telemetry and analyzer verdicts, tracks incident
state, writes an event history, and renders that history in an optional
Streamlit dashboard. It is kept separate from the API and analyzer so it can be
integrated without changing their existing contracts.

## Structure

| File | Responsibility |
| --- | --- |
| `models.py` | Alert fields, severity, and event types |
| `evaluator.py` | Converts router `ToolResult` telemetry into evidence-backed alerts |
| `bridge.py` | Converts the analyzer's `Verdict` object or dictionary into an alert |
| `state_tracker.py` | Suppresses duplicate incidents and emits recovery transitions |
| `store.py` | Validates and atomically persists JSON history; restores tracker state |
| `formatter.py` | Produces plain-language and terminal alert formats |
| `monitor.py` | Polls BGP, interface, TCP, and configuration tools and records events |
| `dashboard.py` | Displays event history and refreshes automatically every five seconds |
| `requirements.txt` | Optional dashboard-only dependency |

## Compatibility contract

The evaluator consumes the current tool result model from
`tools.base_tool.ToolResult`:

```text
tool_id, host, command, success, raw_output, parsed, error
```

This matches the tool result returned by the current tools API. Convert API
response dictionaries to `ToolResult` objects before passing them to
`AlertEvaluator.evaluate`; failed tool results are preserved as telemetry
failure alerts and their parsed output is not treated as trusted evidence.

The analyzer bridge accepts either the current analyzer `Verdict` dataclass or
its dictionary representation. A verdict marked `resolved` is only suppressed
as healthy when it came from the rules source and includes BGP evidence showing
the queried peer is `Established`. This avoids dropping a resolved diagnosis
that actually describes a fault.

The GitHub `main` API currently does not invoke the alert package, expose alert
routes, or install Streamlit. The alert files therefore do not change
application-owned API files or root dependencies. To display analyzer events
through that API, the application owner must explicitly wire the calls
described under [API integration](#api-integration). This integration change
is not included here.

## Installation

From the repository root, install the existing application dependencies:

```powershell
python -m pip install -r requirements.txt
```

Install the optional dashboard dependency only when using the UI:

```powershell
python -m pip install -r alerts/requirements.txt
```

The component uses the repository's existing router tools and their SSH
configuration. Review `tools/device_client.py` and configure credentials
appropriate for the target environment before monitoring real devices; do not
use lab defaults in production.

## Running

Start a single-peer monitor from the repository root:

```powershell
python -m alerts.monitor --host <router-management-address> --peer <bgp-peer-address> --interval 15
```

Start the optional dashboard in another terminal:

```powershell
python -m streamlit run alerts/dashboard.py
```

The monitor and dashboard share `alerts_log.json` at the repository root.
Events are append-only; repeat incidents with unchanged state are suppressed
by the monitor, and a subsequent healthy poll records a `RECOVERY` event. The
dashboard derives active incident counts from the latest event for each
device/peer pair. A corrupt history file is reported as an error rather than
silently overwritten.

For a different history location, pass a `Path` to `NetworkMonitor` or to the
store functions. Keep the monitor and dashboard configured to use the same
file.

## API integration

The following is an integration outline, not a change made to the existing API.
Create one evaluator and state tracker for the application process, restore
their last state at startup, and use the same instances for analyzer and
telemetry events:

```python
from alerts.bridge import verdict_to_alert
from alerts.evaluator import AlertEvaluator
from alerts.state_tracker import AlertStateTracker
from alerts.store import append_alert, restore_tracker

alert_evaluator = AlertEvaluator()
alert_tracker = AlertStateTracker()
restore_tracker(alert_tracker)
```

For direct tool checks, convert the API's tool result payloads into the
existing `ToolResult` model and pass them to `evaluate`. Send a returned alert
through `check_and_update`; when evaluation returns `None`, call
`record_healthy(device, peer)` and persist any resulting recovery. Persist each
non-`None` event with `append_alert`.

For analyzer results, call `verdict_to_alert(verdict, device, peer)`. Persist
fault alerts through `check_and_update`. When it returns `None` for a healthy
verdict, call `record_healthy` and persist any recovery event. This preserves
deduplication and recovery behavior across both event sources.

The tracker is in-memory and should be shared by the event handlers in a
single API process. The JSON store protects file writes across processes, but
it does not coordinate separate in-memory trackers; multi-worker deployments
should use a shared state store or otherwise coordinate alert ownership.

## Event and error behavior

- BGP state, interface-down, TCP/179 reachability, and configuration drift
  checks can produce alerts. Drift is only considered meaningful when a
  baseline is present.
- Failed tool calls produce explicit telemetry-failure alerts; their failed
  parsed values are excluded from evaluation.
- An unchanged active incident is not emitted repeatedly. A healthy result
  after an incident emits a `RECOVERY` event, displayed as `RESOLVED`.
- The dashboard refreshes automatically and has no polling-delay selector,
  manual cycling, or blocking sleep loop.
- Store read/write failures are raised and shown to the dashboard or monitor;
  malformed history is not silently reset.

## Verification

Run the alert component tests from the repository root:

```powershell
python -m pytest tests/alerts
```
