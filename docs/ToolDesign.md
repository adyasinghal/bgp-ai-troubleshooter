# Tool Design — Auto-Triage Troubleshooter

(Updated from ToolDesign_Meeting5. Same idea, now reflecting the 4 built tools
and the escalation chain.)

## Example

```
Input  >>  My BGP peer is stuck at active state, tell me why
```

## Auto-triage flow (with pipeline stages)

```
Input (user question)
   |
   v
[Triage]   read the question -> pick a starting intent            (analyzer/triage.py)
   |
   v
[Rules DB] intent -> which tool to call, and what to try next     (rules_db/)
   |
   v
[Tool]     Connect + Collect + Parse: fetch + clean device data   (tools/  -> REST API)
   |
   v
[ANALYZE]  read the tool's parsed output and decide:              (analyzer/rules_engine.py)
             - fault found?      -> stop, build the verdict (+ ML second opinion)
             - session healthy?  -> stop, report "no fault"
             - inconclusive?     -> follow next_intent_on_fail, loop back to Rules DB
   |
   |  rule chain ended with no root cause
   v
[ML]       classify all collected evidence into a fault class     (analyzer/ml_engine.py)
             - confidence >= 0.7 and not "unknown" -> stop, build the verdict
             - otherwise -> pass its best guess to the LLM as a hint
   |
   v
[LLM]      Claude reads question + all tool output + ML guess     (analyzer/llm_escalation.py)
             - returns root cause, fix, confidence, next checks
             - no API key / API error -> skip, verdict stays unresolved
   |
   v
[Verdict]  root cause + suggested fix + who decided               (analyzer/verdict.py)
           (rules / ml / llm), confidence, next checks
   |
   v
Output (answer to the user; unresolved -> hand to a human)
```

**Analyze is the loop's brain.** The tools only report facts; Analyze interprets them, decides whether a fault is found, and drives the escalation until the case is resolved or the chain ends. Each later stage only runs when the one before it couldn't decide: rules are free and exact, ML is free but probabilistic, and the LLM is the most flexible but costs money per call.

The auto-triage has access to multiple tools and calls them based on the rules in the rule book.

## Rule book

```
rule1: BGP state check request        -> call tool1 (bgp_state)
rule2: Interface check request        -> call tool2 (interface)
rule3: TCP/port reachability request  -> call tool3 (tcp_port)
rule4: Config drift check request     -> call tool4 (config)
```

Escalation chain (what to try next if a tool doesn't resolve the case):

```
bgp_state_check --(if unresolved)--> interface_check
interface_check --(if unresolved)--> tcp_port_check
tcp_port_check  --(if unresolved)--> config_check
config_check    --(if unresolved)--> ML engine
ML engine       --(if confidence < 0.7 or "unknown")--> LLM (Claude)
LLM             --(if unresolved)--> human, with the LLM's suggested next checks
```

- **ML engine** (`analyzer/ml_engine.py`): a scikit-learn RandomForest that reads
  all the collected evidence at once and names a fault class (remote-as mismatch,
  neighbor shut down, neighbor missing, device unreachable, ...) with a
  confidence. It also gives a second opinion next to every rule verdict.
  Trained on synthetic cases for now; retrain with real labelled cases via
  `python3 -m analyzer.ml_engine train --cases cases.jsonl`.
- **LLM escalation** (`analyzer/llm_escalation.py`): sends the question, every
  tool's parsed + raw output, and the ML guess to Claude, which returns a
  structured root cause, fix, confidence and next checks. Needs
  `ANTHROPIC_API_KEY`; without it the verdict says the step was skipped.

## Tools list

```
tool1 (BGP state)  -> runs "show bgp summary"    -> sends each peer's session state
tool2 (Interface)  -> runs "show interface detail" -> sends link/admin state per interface
tool3 (TCP / port) -> checks TCP port 179 to peer  -> sends transport reachability (up/down)
tool4 (Config)     -> runs "show running-config"   -> diffs vs baseline, sends drift status
```

Each tool is reached over REST at:

```
POST /tools/bgp/state        {"host": "...", "peer": "..."}
POST /tools/interface/detail {"host": "...", "interface": "..."}   # interface optional
POST /tools/tcp/check        {"host": "...", "peer_ip": "...", "port": 179}
POST /tools/config/diff      {"host": "..."}
```

And the rule book is queried over REST at:

```
GET /rules/{intent}   -> tools to call + next_intent_on_fail
GET /rules/intents    -> all known intents
GET /rules/tools      -> all tools in the cohort
```

## Example output

```
Output >>  Your interface state is down. That is why your BGP state is stuck
           at active. Please bring the interface up to resolve the issue.
```