# Real-Time AI Network Troubleshooter for BGP Switches

When a BGP session between two routers goes down, finding out why usually means an engineer logging in, running a series of `show` commands and reading through the output. This project is an AI troubleshooter that does that work: you describe the problem in plain language, it investigates the switch itself and tells you the root cause and how to fix it.

```
Input  >>  My BGP peer is stuck at active state, tell me why
Output >>  Neighbor 172.20.20.3 is administratively shut down on this router.
           Fix: router bgp 65001 / no neighbor 172.20.20.3 shutdown
```

The aim is a tool an engineer can attach to a misbehaving BGP device and simply follow its recommendation. It's being built in phases:

1. **Configuration faults** (current): diagnose misconfigurations such as a shut down neighbor, a wrong remote AS, a down interface, a blocked BGP port or a route-map filtering a prefix.
2. **Log-based detection**: find anomalies from event and support logs.
3. **Real-time monitoring**: collect data periodically and suggest fixes as problems appear, with a dashboard for alerts.

It combines deterministic rules, an ML classifier and an LLM, so it stays accurate on known faults while still being able to reason about new ones. It's developed and tested on FRRouting routers in a Containerlab lab.

Built as part of the HPE CPP-2 program.

## Contents

1. [How it works](#1-how-it-works)
2. [Rule book](#2-rule-book)
3. [Tools and API](#3-tools-and-api)
4. [One-time setup](#4-one-time-setup)
5. [Running it](#5-running-it)
6. [Trying out faults](#6-trying-out-faults)
7. [Configuration](#7-configuration)
8. [Troubleshooting](#8-troubleshooting)
9. [Repo structure](#9-repo-structure)

---

## 1. How it works

```
Question + host + peer
   |
   v
Analyzer (analyzer/)  --HTTP-->  REST API (api/)  --SSH or REST-->  Router (FRR)
   |                                |
   |                                +-- Rules DB (rules_db/): the rule book and tool catalog
   v
Verdict: root cause, fix, who decided, confidence, next checks
```

The tools only report facts (parsed CLI output). The analyzer interprets them. It has two modes.

### Agent mode (default)

The LLM decides which tool to run, reads each result, and decides what to check next or concludes.

```
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
   |       [Rule finding] the rule book's verdict on this result  (analyzer/rules_engine.py)
   |                    root_cause -> "conclude now"
   |                    healthy    -> conclude, or go on to routes for a route question
   |                    continue   -> a hint, or the next rule to try
   |                                                            |
   |<------------------------ loop (at most --max-steps) -------+
   |
   |-- conclude (or the step budget is spent)
   v
[Verdict]  root cause, fix, fault class, confidence, next checks, the step
           trace, whether the rules agree, ML second opinion    (analyzer/verdict.py)
```

- Guardrails are in code: catalogued tools only, validated args, host/peer from the CLI, `bgp_state` first, no repeats, a step budget.
- A question about a route or prefix is detected before the first step. The prefix in the question is filled into `route_table` / `prefix_list` if the LLM leaves it out.
- The verdict notes whether the LLM's fault class agrees with the rules. `Rules agree: NO` means the LLM overrode a rule, so check its steps.
- If the LLM is down or its answer is unusable, the run falls back to rules mode.

### Rules mode (`--mode rules`, and the agent's fallback)

A fixed chain that stops at the first stage that can decide:

```
[Triage]   read the question -> starting rule; a route question      (analyzer/triage.py)
           starts at bgp_state and goes on to the route rules
   |
   v
[Rules DB] rule -> which tool to call, and which rule to try next   (rules_db/)
   |
   v
[Tool]     Connect + Collect + Parse                                (tools/ -> REST API)
   |
   v
[Analyze]  read the tool's parsed output and decide:                (analyzer/rules_engine.py)
             - fault found?      -> stop, build the verdict (+ ML second opinion)
             - session healthy?  -> stop, report "no fault"
             - inconclusive?     -> go to the next rule
   |
   |  chain ended with no root cause (a tentative finding is used if there is one)
   v
[ML]       classify all collected evidence into a fault class       (analyzer/ml_engine.py)
             - confidence >= 0.7 and not "unknown" -> stop
             - otherwise -> pass its best guess to the LLM as a hint
   |
   v
[LLM]      reads question + all tool output + ML guess              (analyzer/llm_escalation.py)
             - returns root cause, fix, confidence, next checks
             - unreachable -> skip, verdict stays unresolved
   |
   v
[Verdict]  root cause + fix + who decided (rules / ml / llm)        (analyzer/verdict.py)
           unresolved -> hand to a human with the next checks
```

Each later stage only runs when the one before it couldn't decide: rules are exact, ML is probabilistic, and the LLM is the most flexible but the slowest.

Some findings aren't proof on their own (a TCP/179 timeout, a missing prefix, an MD5 password in the config). These are kept as *tentative* and only used if the chain finds nothing more specific.

### ML engine

A scikit-learn RandomForest (`analyzer/ml_engine.py`) that reads all the collected evidence at once and names a fault class with a confidence. In rules mode it decides when the chain can't; in both modes it gives a second opinion next to the verdict, and the agent can call it as a tool (`ml_classify`).

It knows 17 classes: `healthy`, `interface_down`, `no_route_to_peer`, `tcp_unreachable`, `peer_not_listening`, `auth_mismatch`, `remote_as_mismatch`, `router_id_conflict`, `capability_mismatch`, `hold_timer_expired`, `max_prefix_exceeded`, `afi_not_negotiated`, `neighbor_shutdown`, `neighbor_missing`, `config_drift`, `device_unreachable` and `unknown`. Route questions (`route_filtered`, `route_not_selected`, ...) are left to the rules and the LLM.

It's trained on synthetic cases for now, so its confidence is indicative. The LLM treats its guess as a hint, not a fact.

### LLM

A free local model (`qwen2.5:7b`) served by Ollama by default, or Claude. All LLM calls go through `analyzer/llm_client.py`. In agent mode the LLM is called once per step (a few seconds each); in rules mode only when the rules and ML are stuck.

---

## 2. Rule book

The rule book lives in `rules_db/schema.sql` and is loaded into SQLite (`rules_db/rules.db`) on start. Bump `PRAGMA user_version` at the bottom after any change and the DB is rebuilt. Each rule names a tool, its symptoms, likely causes, how to verify the fix, and the rule to try next if it doesn't resolve the case.

### Chains

`bgp_state` picks the next rule from the state it sees. A session that isn't Established reads the peer's NOTIFICATION / last reset reason first, then works down the stack:

```
bgp_state_<state> -> bgp_neighbor_detail -> interface_check -> route_to_peer_check
                  -> tcp_port_check -> config_check

bgp_state_established -> route_received_not_selected -> route_map_check
                      -> prefix_list_check -> config_check
```

### Rules

| Intent | Tool | Covers | Next |
|---|---|---|---|
| `bgp_state_check` | bgp_state | Any BGP question; entry point when the state isn't known | `bgp_neighbor_detail` |
| `bgp_state_idle` | bgp_state | `Idle (Admin)`: shut down; `Idle (PfxCt)`: max-prefix; plain Idle: OPEN rejected, neighbor missing, no route | `bgp_neighbor_detail` |
| `bgp_state_connect` | bgp_state | TCP not forming: unreachable, port blocked, peer not listening, MD5 mismatch | `bgp_neighbor_detail` |
| `bgp_state_active` | bgp_state | TCP failed: interface down, wrong address/subnet, no route, firewall, MD5, TTL-security | `bgp_neighbor_detail` |
| `bgp_state_opensent` | bgp_state | Bad Peer AS, Bad BGP Identifier, Unsupported Capability, hold time < 3s | `bgp_neighbor_detail` |
| `bgp_state_openconfirm` | bgp_state | Hold timer expires before the first KEEPALIVE; peer resets after OPEN | `bgp_neighbor_detail` |
| `bgp_state_established` | bgp_state | Session up but routes wrong/missing; `(Policy)`; `NoNeg` | `route_received_not_selected` |
| `bgp_neighbor_detail` | bgp_neighbor | The NOTIFICATION / last reset reason; 0 connections ever = fault is below BGP | `interface_check` |
| `interface_check` | interface | The interface on the peer's subnet: admin down, no carrier, no/wrong address, MTU | `route_to_peer_check` |
| `route_to_peer_check` | route_to_peer | No route to the peer, or only the default route matches | `tcp_port_check` |
| `tcp_port_check` | tcp_port | Refused (bgpd down / REJECT), timeout (DROP / loss), no route | `config_check` |
| `config_check` | config | remote-as, update-source, multihop, MD5, TTL-security, timers, max-prefix vs. baseline | — |
| `route_received_not_selected` | route_table | Received but not best: local-pref, AS-path, MED, next hop inaccessible, AS loop, not advertised | `route_map_check` |
| `route_best_path_selection` | route_table | How a path was chosen: weight → local-pref → local → AS-path → origin → MED → eBGP → IGP metric → age → router-id | `route_map_check` |
| `route_leading_case` | route_table | Which path is winning and why | `route_map_check` |
| `route_map_check` | route_map | Deny entry, implicit deny, wrong direction, undefined route-map, set clauses | `prefix_list_check` |
| `prefix_list_check` | prefix_list | Deny entry, exact-length match without ge/le, implicit deny | `config_check` |
| `route_ignore_case` | route_table | Filtered on purpose (e.g. RFC1918); not a fault | — |
| `max_prefix_exceeded` | bgp_neighbor | `Idle (PfxCt)`, Maximum Number of Prefixes Reached | `config_check` |
| `bgp_timers_mismatch` | bgp_neighbor | Timers differ between peers; not a fault (BGP uses the lower hold time) | — |
| `bgp_session_flapping` | interface | Up/Down keeps resetting; unstable link, hold timer expiries | `bgp_neighbor_detail` |

Read any rule over the API with `GET /rules/{intent}`.

---

## 3. Tools and API

Each tool runs one CLI command on the router, parses the output, and returns structured JSON.

| Tool | Command | Returns |
|---|---|---|
| `bgp_state` | `show bgp summary` | Each peer's state and reason (`Admin`, `PfxCt`, `Policy`, `NoNeg`), Up/Down time, router-id; IPv4 and IPv6 peers |
| `bgp_neighbor` | `show bgp neighbors <peer>` | AS numbers, state, shutdown, connections, last reset and NOTIFICATION; on Bad Peer AS, the AS the peer really uses |
| `interface` | `show interface [name]` | Admin state, link state (carrier), IP addresses, MTU, link up/down counts |
| `route_to_peer` | `show ip route <peer>` | Whether there's a route to the peer, its prefix, protocol, next hop and interface |
| `tcp_port` | TCP connect to port 179 | Reachable or not, and why: `refused`, `timeout` or `no_route` |
| `config` | `show running-config` | Diff against a saved baseline, and its severity: `none`, `cosmetic` (descriptions only) or `bgp` |
| `route_table` | `show ip bgp [prefix]` | Each path, which is best and why, the attributes compared, valid / multipath / internal flags |
| `route_map` | `show route-map [name]` | Each entry's permit/deny, matches, sets and how often it matched |
| `prefix_list` | `show ip prefix-list [name]` | Each entry; with a prefix, which entry matches it (or the implicit deny) |

Start the API with `python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000`. Tools:

```
POST /tools/bgp/state          {"host": "...", "peer": "..."}
POST /tools/bgp/neighbor       {"host": "...", "peer": "..."}
POST /tools/interface/detail   {"host": "...", "interface": "..."}   # interface optional
POST /tools/route/peer         {"host": "...", "peer": "..."}
POST /tools/tcp/check          {"host": "...", "peer_ip": "...", "port": 179}
POST /tools/config/diff        {"host": "..."}
POST /tools/route/table        {"host": "...", "prefix": "..."}      # prefix optional
POST /tools/route/map          {"host": "...", "name": "..."}        # name optional
POST /tools/route/prefix-list  {"host": "...", "name": "...", "prefix": "..."}   # both optional
```

Rule book:

```
GET /rules/{intent}   -> tools to call, next_intent_on_fail, symptoms, likely causes, verification
GET /rules/intents    -> all known intents
GET /rules/tools      -> all tools
GET /rules/catalog    -> every tool (args, when to use it) and every rule, in one call
GET /health
```

---

## 4. One-time setup

The lab runs on a Mac inside an OrbStack Linux VM:

- **FRR**: open-source routing suite, the software that actually speaks BGP.
- **Containerlab**: spins up multi-router topologies from containers.
- **OrbStack**: a fast, lightweight VM and container runtime for macOS.

```
macOS (arm64)
├── Ollama (local LLM)
└── OrbStack VM: Ubuntu arm64  ← real Linux kernel
    ├── dockerd
    │   ├── container: router1 (FRR)
    │   └── container: router2 (FRR)
    └── containerlab (a CLI, not a daemon)
```

Everything runs inside the VM, because the routers are only reachable from there. The one exception is Ollama, which runs on the Mac so it can use the GPU. OrbStack mounts your Mac home folder in the VM at the same path, so the repo is at the same path in both.

### On the Mac

```bash
# Homebrew, if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

brew install --cask orbstack
orb create ubuntu clab
orb -m clab     # drops you into a shell inside the VM
```

### Inside the VM

```bash
sudo apt update && sudo apt install -y curl ca-certificates git python3-pip python3-yaml

# Docker engine
curl -fsSL https://get.docker.com | sudo bash
sudo usermod -aG docker $USER

# Containerlab
sudo bash -c "$(curl -sL https://get.containerlab.dev)"
containerlab version
docker run --rm quay.io/frrouting/frr:8.5.2 uname -m

# Log out and back in so the docker group applies
exit
orb -m clab
```

Python packages, from the repo root (re-run whenever `requirements.txt` changes):

```bash
cd /path/to/bgp-ai-troubleshooter
python3 -m pip install -r requirements.txt --break-system-packages
```

### Router image (FRR + SSH)

The plain FRR image has no SSH server, so the tools can't reach it. `lab/containerlab/Dockerfile` builds an image with FRR, an SSH server, an `admin`/`admin` login, and the BGP daemon enabled at boot:

```bash
cd lab/containerlab
docker build -t frr-ssh:8.5.2 .
cd ../..
```

Re-run this only if you edit the Dockerfile. A rebuild doesn't reach running containers, so destroy the lab first and the next deploy recreates them:

```bash
sudo containerlab destroy -t lab/containerlab/topology.clab.yml
```

The lab is defined in `lab/containerlab/topology.clab.yml`: two routers from this image, with pinned management IPs (router1 = 172.20.20.2, router2 = 172.20.20.3) and an `eth1` link between them.

### Ollama (on the Mac)

In a normal macOS terminal, not `orb -m clab`:

```bash
brew install ollama
brew services start ollama          # runs the Ollama server now and at login
ollama pull qwen2.5:7b              # the default model
ollama run qwen2.5:7b "say hi"
```

Or install the Ollama app from ollama.com and open it instead of `brew services start`. Without Ollama the analyzer still works: agent mode falls back to the rule chain.

---

## 5. Running it

Use two terminals, both inside the VM (`orb -m clab`) and in the repo root:

- **Terminal 1**: runs the REST API (stays open).
- **Terminal 2**: runs the analyzer.

### Step 1: Deploy the lab

```bash
sudo containerlab deploy -t lab/containerlab/topology.clab.yml
sudo containerlab inspect -t lab/containerlab/topology.clab.yml   # check the IPs
```

### Step 2: Configure the BGP neighbors

Neighbor config isn't saved across a redeploy, so do this after every deploy:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"
docker exec clab-bgp-lab-router2 vtysh -c "conf t" -c "router bgp 65002" -c "neighbor 172.20.20.2 remote-as 65001" -c "end"
```

Wait ~10s, then check it's `Established`:

```bash
ssh admin@172.20.20.2 'vtysh -c "show bgp summary"'   # password: admin
```

### Step 3: Start the REST API (Terminal 1)

```bash
python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Leave it running. You should see `Application startup complete.`

### Step 4: Smoke-test the API (Terminal 2)

```bash
curl http://localhost:8000/health
curl http://localhost:8000/rules/bgp_state_check
curl -X POST http://localhost:8000/tools/bgp/state \
  -H "Content-Type: application/json" \
  -d '{"host": "172.20.20.2", "peer": "172.20.20.3"}'
```

Expect `{"status":"ok"}`, the rule JSON, then `"success": true` with a populated `parsed.peers`.

### Step 5: Check the LLM

The analyzer in the VM reaches Ollama on the Mac at `http://host.orb.internal:11434`:

```bash
curl http://host.orb.internal:11434/api/tags   # should list qwen2.5:7b
```

### Step 6: Run the analyzer

```bash
python3 -m analyzer.run "My BGP peer is stuck at active state" \
  --host 172.20.20.2 --peer 172.20.20.3
```

With BGP up, the agent checks `bgp_state`, sees `Established` and concludes (~20s with qwen2.5:7b). For example:

```
Resolved:      True
Root cause:    BGP peer 172.20.20.3 is Established
Suggested fix: No action needed — the session is up.
Decided by:    agent (confidence high)
Tools checked: bgp_state
Triage:        BGP peer stuck at active state (suspects: tcp_unreachable, remote_as_mismatch, neighbor_shutdown, config_drift)
Fault class:   healthy
Rules agree:   yes
ML opinion:    healthy (1.00)
Steps:
  1. bgp_state -> peer Established (Policy) [rule: healthy]
     why: We need to start by checking the BGP session state ...
Note:          Diagnosed by qwen2.5:7b
```

- `Triage` is the LLM's reading of the question before any tool ran. An off-topic question stops there.
- `[rule: ...]` marks a result the rule book decided on its own.
- Wording and tool order vary between runs.

With `--mode rules`:

```
Resolved:      True
Root cause:    BGP peer 172.20.20.3 is Established, but it exchanges no routes: no route policy for this eBGP peer ((Policy): bgp ebgp-requires-policy)
Suggested fix: No action needed for the session. If routes from this peer are expected, add a route-map in/out for the neighbor.
Decided by:    rules
Tools checked: bgp_state
Fault class:   healthy
ML opinion:    healthy (1.00)
```

### Options

| Flag | Effect |
|---|---|
| `--mode agent` / `--mode rules` | LLM agent (default) or the fixed rule chain |
| `--max-steps N` | Agent: most tool calls before it must conclude (default 6) |
| `--trust-rules` | Agent: stop as soon as a rule finds a root cause or a healthy session |
| `--no-ml` | Skip the ML engine |
| `--no-llm` | Skip the LLM entirely (implies `--mode rules`) |
| `--url` | Where the REST API is (default `http://localhost:8000`) |
| `--log-dir` | Where to write the run log (default `logs/`) |

Every run writes a log to `logs/run_<date>-<time>.log` (the path is printed as the last line). It records each step: agent decisions, every tool call with its payload, result and raw output, rule findings, the ML prediction, LLM calls and token usage, and the final verdict. If a run crashes, the traceback is there too.

The first run trains the ML model (about a second) and saves it to `analyzer/models/`. Later runs reuse it.

---

## 6. Trying out faults

Inject a fault, ask the analyzer about it, then undo it. Outputs below are from rules mode; the agent reaches the same answers in its own words.

### Neighbor shut down

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 shutdown" -c "end"

python3 -m analyzer.run "My BGP peer is stuck at active state" --host 172.20.20.2 --peer 172.20.20.3
```

`bgp_state` shows `Idle (Admin)` (not `Active` as the question says), which the rule book treats as a shut down neighbor:

```
Root cause:    Neighbor 172.20.20.3 is administratively shut down on this router (Idle (Admin))
Suggested fix: router bgp <local-asn> / no neighbor 172.20.20.3 shutdown
Decided by:    rules
Tools checked: bgp_state
Fault class:   neighbor_shutdown
```

Undo:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "no neighbor 172.20.20.3 shutdown" -c "end"
```

### Wrong remote AS

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65009" -c "end"
```

Wait ~10s, then:

```bash
python3 -m analyzer.run "My BGP peer won't come up" --host 172.20.20.2 --peer 172.20.20.3
```

The session is `Idle`, so the next check is `bgp_neighbor`. It shows router1 sent a Bad Peer AS NOTIFICATION, and reads the AS router2 really uses from its OPEN:

```
Root cause:    This router expects AS 65009 for 172.20.20.3, but the peer uses AS 65002
Suggested fix: router bgp 65001 / neighbor 172.20.20.3 remote-as 65002
Tools checked: bgp_state -> bgp_neighbor
Fault class:   remote_as_mismatch
```

The same tool catches a neighbor removed on router2 (`Cease/Peer De-configured`), a duplicate router-id, a capability mismatch and max-prefix teardowns.

Undo:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"
```

### A missing route (route-map filtering)

The sessions so far exchange no routes. Have router2 advertise two prefixes, and give router1 an inbound route-map that drops one of them:

```bash
docker exec clab-bgp-lab-router2 vtysh -c "conf t" -c "router bgp 65002" -c "no bgp ebgp-requires-policy" -c "no bgp network import-check" -c "address-family ipv4 unicast" -c "network 192.168.10.0/24" -c "network 192.168.20.0/24" -c "end"
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "ip prefix-list BLOCK20 seq 5 permit 192.168.20.0/24" -c "route-map FROM-R2 deny 10" -c "match ip address prefix-list BLOCK20" -c "exit" -c "route-map FROM-R2 permit 20" -c "exit" -c "router bgp 65001" -c "no bgp ebgp-requires-policy" -c "address-family ipv4 unicast" -c "neighbor 172.20.20.3 route-map FROM-R2 in" -c "end"
```

`show bgp summary` on router1 now shows a prefix count instead of `(Policy)`. Ask about the missing prefix:

```bash
python3 -m analyzer.run "Why don't I get 192.168.20.0/24 from router2?" --host 172.20.20.2 --peer 172.20.20.3
```

The session is Established, but the question is about a route, so it keeps going:

```
Root cause:    Route-map deny entries are filtering routes: FROM-R2 seq 10 (ip address prefix-list BLOCK20, 2 match(es))
Tools checked: bgp_state -> route_table -> route_map
Fault class:   route_filtered
```

In agent mode the steps read:

```
  1. bgp_state -> peer Established
  2. route_table -> 192.168.20.0/24 not in the BGP table
  3. route_map -> 2 entries, denying: FROM-R2 10 [rule: root_cause]
```

Comparing two paths to the same prefix (best-path selection) needs a third router, which this lab doesn't have.

Undo:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "address-family ipv4 unicast" -c "no neighbor 172.20.20.3 route-map FROM-R2 in" -c "exit-address-family" -c "bgp ebgp-requires-policy" -c "exit" -c "no route-map FROM-R2" -c "no ip prefix-list BLOCK20" -c "end"
docker exec clab-bgp-lab-router2 vtysh -c "conf t" -c "router bgp 65002" -c "address-family ipv4 unicast" -c "no network 192.168.10.0/24" -c "no network 192.168.20.0/24" -c "exit-address-family" -c "bgp ebgp-requires-policy" -c "bgp network import-check" -c "end"
```

### Save a config baseline (optional)

The `config` tool can only report drift against a known-good baseline. With BGP healthy, save one per router:

```bash
python3 -c "
from tools.config import ConfigTool
t = ConfigTool()
for host in ['172.20.20.2', '172.20.20.3']:
    t.save_baseline(host, t.device_client.run_vtysh(host, 'show running-config').output)
"
```

Baselines are saved in `tools/config_baselines/`. From then on the config check lists what changed. A change to descriptions only is reported as cosmetic, not a fault, and a drift that confirms a more specific finding (e.g. an MD5 password) is added to that finding.

### Retrain the ML model (optional)

The model is trained on synthetic cases. To retrain, and to add real labelled cases (one JSON object per line, `{"evidence": [...], "peer": "172.20.20.3", "label": "remote_as_mismatch"}`):

```bash
python3 -m analyzer.ml_engine train                    # synthetic only
python3 -m analyzer.ml_engine train --cases cases.jsonl
```

It prints per-class precision/recall on a held-out split, then saves the model. If the features or fault classes in `ml_engine.py` change, an old saved model is retrained automatically on the next run.

---

## 7. Configuration

### LLM

The defaults work as-is; set these only to change them (all in `analyzer/llm_client.py`):

```bash
export BGP_LLM_MODEL=qwen2.5:7b                      # any model you've pulled
export BGP_LLM_URL=http://host.orb.internal:11434    # where Ollama is
export BGP_LLM_NUM_CTX=16384                         # context window (tokens)
export BGP_LLM_TIMEOUT=300                           # seconds to wait for an answer
```

To use Claude instead, set `BGP_LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. The model defaults to `claude-opus-5-5`; change it with `BGP_LLM_MODEL`.

### Device transport

The tools reach routers over SSH by default (Paramiko, running `vtysh` on the router). For devices that take CLI commands over HTTP instead, set these before starting the API:

```bash
export DEVICE_TRANSPORT=rest        # "ssh" (default) or "rest"
export DEVICE_REST_PORT=8080        # default 8080
export DEVICE_REST_SCHEME=http      # default http
```

The REST client sends `POST <scheme>://<host>:<port>/command` with `{"type": "vtysh" | "shell", "command": "..."}` and reads `{"success", "output", "error"}` back.

`lab/mock_switch.py` is a stand-in REST device for trying this without hardware. It answers `show bgp summary`, `show interface` and `show running-config` with canned FRR output:

```bash
python3 lab/mock_switch.py                                   # listens on port 9000
DEVICE_TRANSPORT=rest DEVICE_REST_PORT=9000 \
  python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000
python3 -m analyzer.run "Is my BGP peer up?" --host 127.0.0.1 --peer 172.20.20.3 --mode rules
```

### Reaching the routers by hand

Over SSH (`admin` / `admin`), quoting the whole remote command:

```bash
ssh admin@172.20.20.2 'vtysh -c "show bgp summary"'
```

Or with `docker exec`, no SSH needed:

```bash
docker exec -it clab-bgp-lab-router1 vtysh
```

`-it` makes it interactive, `clab-bgp-lab-router1` is the container Containerlab created for router1, and `vtysh` is FRR's shell, where you type commands like `show ip bgp summary` as on a physical switch.

---

## 8. Troubleshooting

- **`Address already in use`**: an old API is still running, often in another terminal tab. Stop it with Ctrl-C there, or with `pkill -f "uvicorn api.main:app"` (`ss -ltnp | grep :8000` shows what holds the port). Or run on `--port 8001` and pass `--url http://localhost:8001` to the analyzer.
- **`Unable to connect to port 22`** or **`The diagnostic tools could not reach the router`**: the lab isn't deployed, or the IP is wrong. Check with `containerlab inspect`.
- **`% Can't open configuration file /etc/frr/vtysh.conf`**: harmless; the command still ran. The current Dockerfile creates this file, so rebuild the image and redeploy to get rid of it.
- **`WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!`** on `ssh admin@172.20.20.x`: expected after rebuilding the image, which makes new SSH host keys. Clear them with `ssh-keygen -R 172.20.20.2 && ssh-keygen -R 172.20.20.3`. To skip this for the lab subnet, add to `~/.ssh/config` in the VM: `Host 172.20.20.*` / `StrictHostKeyChecking no` / `UserKnownHostsFile /dev/null`. The analyzer isn't affected.
- **`% Can not configure the local system as neighbor`**: the neighbor IP is the router's own address, which happens when management IPs swap on a deploy. The pinned `mgmt-ipv4` in the topology prevents this; destroy and redeploy if your lab predates it.
- **ML results look off after upgrading scikit-learn**: retrain with `python3 -m analyzer.ml_engine train`.
- **First agent answer takes minutes**: Ollama unloads the model after 5 idle minutes and reloads it on the next call. Later runs are faster.

### Notes

- Always run the API and the analyzer inside the VM and from the repo root.
- Management IPs are pinned in `topology.clab.yml`. Without the `mgmt-ipv4` lines, IPs follow container start order and can swap between deploys.
- The routers peer over the management network, so shutting `eth1` doesn't affect the session, and the rules only blame the interface on the peer's subnet. The classic "interface down → BGP Active" demo needs peering over the `eth1` link.

---

## 9. Repo structure

```
bgp-ai-troubleshooter/
├── analyzer/
│   ├── run.py              # CLI entry point
│   ├── agent.py            # LLM agent: picks each tool, reads results, concludes
│   ├── triage.py           # reads the question (LLM, or keywords in rules mode)
│   ├── prompts.py          # agent prompts, decision schemas, fault classes
│   ├── tool_registry.py    # tool catalog: validates and runs tool calls
│   ├── rules_engine.py     # rule findings, and the rules-mode chain
│   ├── ml_engine.py        # scikit-learn fault classifier
│   ├── llm_client.py       # one entry point for LLM calls (Ollama or Claude)
│   ├── llm_escalation.py   # rules mode: hands unresolved cases to the LLM
│   ├── rest_client.py      # calls the REST API over HTTP
│   └── verdict.py          # the root cause / fix output
├── api/
│   └── main.py             # REST API: tool endpoints and the rule book
├── rules_db/
│   ├── schema.sql          # the rule book and tool catalog
│   └── rules_db.py         # loads it into SQLite
├── tools/
│   ├── base_tool.py  device_client.py          # shared base, SSH / REST transport
│   ├── bgp_state.py  bgp_neighbor.py  interface.py  route_to_peer.py  tcp_port.py  config.py
│   └── route_table.py  route_map.py  prefix_list.py
├── lab/
│   ├── containerlab/
│   │   ├── Dockerfile          # FRR + SSH router image
│   │   └── topology.clab.yml   # two routers, pinned management IPs
│   └── mock_switch.py          # stand-in REST device
├── docs/
│   ├── project_overview.md     # program overview: phases, design, tools used
│   └── Pre-requisites.md       # study notes (TCP/IP basics)
├── requirements.txt
└── README.md
```
