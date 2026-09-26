# Alert integration changes

## Implemented on the `alert` branch

- Terminal rule, ML, LLM, and unresolved analyzer verdicts now pass through
  `alerts.integration.publish_verdict`. This persists fault events and turns
  a subsequent rules verdict with confirmed `Established` BGP evidence into
  a recovery event.
- Monitor and analyzer transitions now use the same lock-protected durable
  state operation. It reloads the most recent device/peer event while holding
  the history lock, so separate processes suppress duplicates and agree on
  whether a recovery is needed.
- The Streamlit dashboard displays current unresolved incidents separately
  from resolved-event history. A recovered incident no longer appears in the
  active incident list.
- Dashboard event timestamps are formatted in Indian Standard Time.
- FRR 8.5.2 interface polling now uses its supported `show interface`
  command.
- TCP reachability now matches the exact `REACHABLE` result, rather than
  mistakenly treating `UNREACHABLE` as reachable.
- Tests cover analyzer publication, recovery, durable duplicate suppression,
  event-list separation, IST formatting, TCP parsing, and FRR interface
  command compatibility.

## Compatibility notes

- No changes to existing `main`-branch API files are required for the alert
  lifecycle; the analyzer wiring is included in this branch.
- The existing API tool and rules endpoint payloads remain unchanged.
- Analyzer `Verdict` structure and CLI output contract remain unchanged.
- The standalone alert dashboard continues to read the repository-root
  `alerts_log.json`; no additional API route is required for it.
- Install the optional dashboard dependency with
  `python -m pip install -r alerts/requirements.txt`.

## Verification

Run the full suite from the repository root:

```powershell
python -m pytest -q
```

The copied isolated package can also run its component tests with:

```powershell
Set-Location test
python -m pytest -q tests/alerts
```
