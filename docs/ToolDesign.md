# Tool Design — Auto-Triage Troubleshooter

(Updated from ToolDesign_Meeting5: 7 tools now, an LLM agent that drives them,
and the rule chain it falls back to.)

## Example

```
Input  >>  My BGP peer is stuck at active state, tell me why
```

## LLM agent flow (default: `--mode agent`)

The LLM decides which tool to run, reads each result, and decides what to
check next or concludes.

```
Input (user question + host + peer)
   |
   v
[Triage]   LLM reads the question: symptom, claimed state,         (analyzer/triage.py)
           suspects, planned checks; off-topic -> stop
   |
   v
[Decide]   LLM reads: question, tool catalog + rule book, all steps  (analyzer/agent.py,
   |       so far -> call ONE tool, or conclude                      analyzer/prompts.py)
   |
   |-- call a tool ---------------------------------------------+
   |                                                            v
   |       [Guardrails] tool in the catalog? args valid? not a repeat?  (analyzer/tool_registry.py)
   |                    rejected -> the reason goes back to the LLM
   |                                                            v
   |       [Tool]       Connect + Collect + Parse               (tools/ -> REST API)
   |                                                            v
   |       [Rule finding] the rule book's verdict on this result  (rules_engine.evaluate)
   |                    root_cause / healthy -> "conclude now"
   |                    continue             -> suggested next intent
   |                                                            |
   |<------------------------ loop (at most --max-steps) -------+
   |
   |-- conclude (or the step budget is spent)
   v
[Verdict]  root cause, fix, fault class, confidence, next checks, the step
           trace, whether the rules agree, ML second opinion    (analyzer/verdict.py)
```

- Guardrails are in code: catalogued tools only, validated args, host/peer
  from the CLI, bgp_state first, no repeats, a step budget.
- The verdict notes whether the LLM's fault class agrees with the rules.
- If the LLM is down or its answer is unusable, the run falls back to the
  rule chain below.
- `--trust-rules` stops at the first rule decision; `--no-ml` drops ML.

## Rule chain flow (`--mode rules`, and the agent's fallback)

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
[LLM]      LLM reads question + all tool output + ML guess        (analyzer/llm_escalation.py)
             - returns root cause, fix, confidence, next checks
             - LLM unreachable / error -> skip, verdict stays unresolved
   |
   v
[Verdict]  root cause + suggested fix + who decided               (analyzer/verdict.py)
           (rules / ml / llm), confidence, next checks
   |
   v
Output (answer to the user; unresolved -> hand to a human)
```

The tools only report facts; Analyze interprets them. Each later stage only
runs when the one before it couldn't decide: rules are exact, ML is
probabilistic, and the LLM is the most flexible but the slowest.

## Rule book

```
rule1: BGP state check request        -> call tool1 (bgp_state)
rule2: Interface check request        -> call tool2 (interface)
rule3: TCP/port reachability request  -> call tool3 (tcp_port)
rule4: Config drift check request     -> call tool4 (config)
rule5: Neighbor detail request        -> call tool5 (bgp_neighbor)
```

Each rule also records its symptoms, likely causes and how to verify the fix
(`GET /rules/{intent}`). Rules 1-4 form the chain below. The rules below (from
Gaurav's v2 rule book) and rule 5 are in the agent's catalog; rules mode
doesn't walk them.

BGP session states:

| Intent | Symptoms | Likely causes | Verification | Next |
|---|---|---|---|---|
| `bgp_state_idle` | Idle, no connection attempts | Neighbor shut down (`Idle (Admin)`) or not configured; no route to peer; AS/peer IP wrong | State moves past Idle within seconds | `interface_check` |
| `bgp_state_connect` | Trying the TCP connection | Peer unreachable at L3; TCP/179 blocked; peer not listening | TCP handshake succeeds, state moves to OpenSent | `tcp_port_check` |
| `bgp_state_active` | TCP failed or refused, retrying | Interface down; wrong peer IP; no route; ACL; MTU mismatch | Interface up, TCP/179 reachable, state moves to OpenSent | `interface_check` |
| `bgp_state_opensent` | OPEN sent, waiting for the peer's OPEN | AS mismatch; version mismatch; hold-timer mismatch; capability failure | NOTIFICATION reason cleared, state moves to OpenConfirm | `bgp_neighbor_detail` |
| `bgp_state_openconfirm` | OPENs exchanged, waiting for KEEPALIVE | MD5/TCP-AO mismatch; hold-timer mismatch; policy reject after OPEN | KEEPALIVE received, Established without flapping | `bgp_neighbor_detail` |
| `bgp_state_established` | Session up, prefixes missing or wrong | Not advertised; inbound route-map; better path elsewhere; max-prefix | Prefix present with the expected next hop and marked best (`>`) | `route_received_not_selected` |

Route selection:

| Intent | Covers | Verification |
|---|---|---|
| `route_received_not_selected` | Route received but not best or not installed | Prefix shows `>` and appears in `show ip route` |
| `route_best_path_selection` | How a path was chosen: weight → local-pref → locally originated → AS-path length → origin → MED → eBGP over iBGP → IGP metric → oldest → router-id | The best path matches that order at the first differing step |
| `route_map_check` | A route-map denying or changing a path | The prefix passes the route-map and its attributes match the set clauses |
| `route_ignore_case` | Routes filtered on purpose (e.g. RFC1918, default route); not a fault | The filtering matches the intended policy |
| `route_leading_case` | Which path is winning and why | The `>` path has the best value at the first differing step |

Escalation chain (what to try next if a tool doesn't resolve the case):

```
bgp_state_check --(if unresolved)--> interface_check
interface_check --(if unresolved)--> tcp_port_check
tcp_port_check  --(if unresolved)--> config_check
config_check    --(if unresolved)--> ML engine
ML engine       --(if confidence < 0.7 or "unknown")--> LLM
LLM             --(if unresolved)--> human, with the LLM's suggested next checks
```

- **ML engine** (`analyzer/ml_engine.py`): a scikit-learn RandomForest that reads
  all the collected evidence at once and names a fault class (remote-as mismatch,
  neighbor shut down, neighbor missing, device unreachable, ...) with a
  confidence. It also gives a second opinion next to every rule verdict.
  Trained on synthetic cases for now; retrain with real labelled cases via
  `python3 -m analyzer.ml_engine train --cases cases.jsonl`.
- **LLM escalation** (`analyzer/llm_escalation.py`): sends the question, every
  tool's parsed + raw output, and the ML guess to the LLM (local Ollama model by
  default, or Claude; see `analyzer/llm_client.py`), which returns a structured
  root cause, fix, confidence and next checks. If the LLM can't be reached, the
  verdict says the step was skipped.
- In agent mode the ML engine is also a tool the LLM can call (`ml_classify`),
  and the rule book is the catalog it chooses from rather than a fixed order.

## Tools list

```
tool1 (BGP state)  -> runs "show bgp summary"    -> sends each peer's session state
tool2 (Interface)  -> runs "show interface"      -> sends link/admin state per interface
tool3 (TCP / port) -> checks TCP port 179 to peer  -> sends transport reachability (up/down)
tool4 (Config)     -> runs "show running-config"   -> diffs vs baseline, sends drift status
tool5 (BGP neighbor) -> runs "show bgp neighbors <peer>" -> sends AS, state, shutdown, last reset
                        and NOTIFICATION; on Bad Peer AS, the AS the peer really uses
tool6 (Route table)  -> runs "show ip bgp [prefix]" -> sends each path, which is best and why,
                        and the attributes compared
tool7 (Route map)    -> runs "show route-map [name]" -> sends each entry's permit/deny, matches,
                        sets and how often it matched
```

Each tool is reached over REST at:

```
POST /tools/bgp/state        {"host": "...", "peer": "..."}
POST /tools/bgp/neighbor     {"host": "...", "peer": "..."}
POST /tools/interface/detail {"host": "...", "interface": "..."}   # interface optional
POST /tools/tcp/check        {"host": "...", "peer_ip": "...", "port": 179}
POST /tools/config/diff      {"host": "..."}
POST /tools/route/table      {"host": "...", "prefix": "..."}      # prefix optional
POST /tools/route/map        {"host": "...", "name": "..."}        # name optional
```

And the rule book is queried over REST at:

```
GET /rules/{intent}   -> tools to call, next_intent_on_fail, symptoms, likely causes, verification
GET /rules/intents    -> all known intents
GET /rules/tools      -> all tools in the cohort
GET /rules/catalog    -> every tool (args, when to use it) and every rule, in one call
```

## Example output

```
Output >>  Your interface state is down. That is why your BGP state is stuck
           at active. Please bring the interface up to resolve the issue.
```