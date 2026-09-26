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
| `integration.py` | Publishes terminal analyzer verdicts through the shared lifecycle |
| `state_tracker.py` | Suppresses duplicate incidents and emits recovery transitions |
| `store.py` | Validates history and atomically coordinates transitions across processes |
| `presentation.py` | Separates active incidents from recovery history and formats IST timestamps |
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

Every terminal result from `analyzer.rules_engine.diagnose` is now published
through `alerts.integration.publish_verdict`. This includes rule, ML, LLM, and
unresolved results. A rules verdict is treated as healthy only when its BGP
evidence confirms `Established`; this closes a previously active alert with a
recovery event. The analyzer's existing `Verdict` response shape is unchanged.

The API tool and rules endpoints keep their existing contracts. An additional
alert-history HTTP endpoint is unnecessary for the standalone dashboard,
which reads the shared history file directly.

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
Events are append-only. The monitor and analyzer both use the durable
transition operation, which locks the history file, reloads the latest
device/peer state, suppresses duplicate incidents, and emits a `RECOVERY`
event when healthy evidence follows a fault. This keeps separate monitor and
analyzer processes coordinated. A corrupt history file is reported as an
error rather than silently overwritten.

The dashboard lists only the latest unresolved incident for each device/peer
under **Active incidents**. Recovery events appear separately under
**Resolved incident history**; an incident that has recovered is no longer
shown as active. Event times are converted from stored UTC timestamps to
Indian Standard Time (IST).

For a different history location, pass a `Path` to `NetworkMonitor` or to the
store functions. Keep the monitor and dashboard configured to use the same
file.

## Event and error behavior

- BGP state, interface-down, TCP/179 reachability, and configuration drift
  checks can produce alerts. Drift is only considered meaningful when a
  baseline is present.
- TCP probes validate the peer as IPv4 and the port as 1–65535 before
  constructing the remote shell command.
- Failed tool calls produce explicit telemetry-failure alerts; their failed
  parsed values are excluded from evaluation.
- An unchanged active incident is not emitted repeatedly. A healthy result
  after an incident emits one `RECOVERY` event and removes it from the active
  incident list.
- Recovery history is shown in its own section, not as an active alert.
- Alert timestamps are displayed in Indian Standard Time.
- Durable transition handling coordinates tracker state between monitor and
  analyzer processes using the alert-history file lock.
- The dashboard refreshes automatically and has no polling-delay selector,
  manual cycling, or blocking sleep loop.
- Store read/write failures are raised and shown to the dashboard or monitor;
  malformed history is not silently reset.

## Verification

Run the alert component tests from the repository root:

```powershell
python -m pytest tests/alerts
```
