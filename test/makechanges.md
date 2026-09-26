# Required changes for full alert integration

## Verified alert-package status

The remote `alert` branch contains the `alerts/` package, alert tests, and the
integration handoff. Its files match the local component copy. The package
imports the existing tool-result model and converts the analyzer's existing
verdict shape. The isolated alert tests and synthetic monitor demo verify the
alert behavior without requiring a live router.

## Changes needed outside `alerts/`

These changes are required to have analyzer results show up in the alert
history and dashboard. They are intentionally not made here because they
modify application-owned integration surfaces:

1. **`analyzer/rules_engine.py` — required:** Send each terminal `Verdict`
   through `verdict_to_alert`. Use a process-wide `AlertStateTracker`, restore
   it once at startup, deduplicate fault events, and persist emitted alerts
   with `append_alert`. When a rules verdict confirms `Established`, call
   `record_healthy` so an earlier incident produces a recovery event. Apply the
   same event path to accepted ML, LLM, and unresolved verdicts.
2. **`api/main.py` — optional:** Add an alert-history endpoint only if API
   clients need to read events over HTTP. The standalone dashboard reads the
   shared history file directly. Keep existing tool and rules response shapes
   unchanged.
3. **`.gitignore` — recommended:** Ignore `alerts_log.json` and
   `alerts_log.json.lock` to avoid committing local runtime state.
4. **Root `requirements.txt` — optional:** Add Streamlit only if the main
   application wants to install the standalone alert dashboard by default.
   Otherwise use `alerts/requirements.txt`.
5. **Integration tests — required with the wiring:** Cover a detected fault,
   healthy Established recovery, duplicate suppression, telemetry failure,
   and any HTTP alert-history endpoint that is introduced.

## Scope limitation

The alert package can be run as a standalone polling monitor and dashboard,
but until item 1 is implemented, calling the existing analyzer does not
automatically publish analyzer verdicts into the alert history. That is the
remaining end-to-end integration gap; it is not a mismatch in the
`ToolResult` or `Verdict` data structures.

## Test-run finding

The first Windows demo run exposed Unicode arrows in state-tracker log messages
that could not be encoded by the default Windows console code page. The
log-only arrows have been replaced with ASCII `->` in
`alerts/state_tracker.py`, so incident changes and recovery are logged without
`UnicodeEncodeError`. Alert state behavior is unchanged.

The copied existing `tools/bgp_state.py` also emits a Python
`SyntaxWarning` from a regex inside its commented-out historical parser. It
does not prevent compilation, imports, tests, or the demo. It is outside the
alert package and can be cleaned up separately if maintainers want to remove
that dead commented block.
