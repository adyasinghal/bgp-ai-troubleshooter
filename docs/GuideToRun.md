# How to Run & Test the BGP AI Troubleshooter

This guide takes you from a fresh machine to running the full workflow:
lab → REST tools → Analyze stage.

The Analyze stage has two modes:

- **agent** (default): the LLM picks each tool, reads the result, and decides
  what to check next or concludes. Falls back to rules mode if the LLM is down.
- **rules** (`--mode rules`): the fixed chain, stopping at the first step that
  finds a root cause:

  ```
  rule chain → ML engine (scikit-learn) → LLM (local model via Ollama) → human
  ```

Everything runs **inside the OrbStack `clab` VM**, because the tool reaches the routers over SSH and those routers are only reachable from inside the VM. The one exception is Ollama, which runs on the Mac.

## Prerequisites (one-time)

Everything in `InitialSetup.md`: the `clab` VM with Docker and Containerlab, the router image, the Python packages, and Ollama with `qwen2.5:7b` on the Mac. Without Ollama the analyzer still works: agent mode falls back to the rule chain and the verdict says so.

## Layout

You'll use two terminals, both inside the VM (`orb -m clab`):
- **Terminal 1** — runs the REST API (stays open).
- **Terminal 2** — runs the tests and the analyzer.

---

## Step 1 — Enter the VM

```bash
orb -m clab
cd /Users/adyasinghal/HPE-CPP/bgp-ai-troubleshooter
```

## Step 2 — Deploy the lab

```bash
sudo containerlab deploy -t lab/containerlab/topology.clab.yml
```

If you changed the Dockerfile, rebuild the image first (see `InitialSetup.md`).

The topology pins the management IPs (`mgmt-ipv4`), so every deploy gives **router1 = 172.20.20.2** and **router2 = 172.20.20.3**, the addresses this guide uses.  
Check them anytime with:

```bash
sudo containerlab inspect -t lab/containerlab/topology.clab.yml
```

## Step 3 — Configure the BGP neighbors

(Neighbor config is not saved across a redeploy; bgpd is already enabled in the image.)

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"
docker exec clab-bgp-lab-router2 vtysh -c "conf t" -c "router bgp 65002" -c "neighbor 172.20.20.2 remote-as 65001" -c "end"
```

Confirm it's up (wait ~10s for `Established`):

```bash
ssh admin@172.20.20.2 'vtysh -c "show bgp summary"'   # password: admin
```

## Step 4 — Start the REST API (Terminal 1)

Run this from the repo root (`bgp-ai-troubleshooter/`), not from `lab/`:

```bash
python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Leave this running. You should see `Application startup complete.`

## Step 5 — Smoke-test the API (Terminal 2)

```bash
# open a second VM shell
orb -m clab
cd /Users/adyasinghal/HPE-CPP/bgp-ai-troubleshooter

curl http://localhost:8000/health
curl http://localhost:8000/rules/bgp_state_check
curl -X POST http://localhost:8000/tools/bgp/state \
  -H "Content-Type: application/json" \
  -d '{"host": "172.20.20.2", "peer": "172.20.20.3"}'
```

Expect: `{"status":"ok"}`, the rules JSON, then `"success": true` with a populated `parsed.peers`.

## Step 6 — Check the LLM

Ollama runs on the Mac; the analyzer in the VM reaches it at `http://host.orb.internal:11434`, which OrbStack forwards to the Mac's localhost. From Terminal 2:

```bash
curl http://host.orb.internal:11434/api/tags   # should list qwen2.5:7b
```

The defaults work as-is; set these only to change them:

```bash
export BGP_LLM_MODEL=qwen2.5:7b                      # any model you've pulled
export BGP_LLM_URL=http://host.orb.internal:11434    # where Ollama is
export BGP_LLM_NUM_CTX=16384                         # context window (tokens)
export BGP_LLM_TIMEOUT=300                           # seconds to wait for an answer
```

To use Claude instead of the local model, set `BGP_LLM_PROVIDER=anthropic` and
`ANTHROPIC_API_KEY` (the model defaults to `claude-opus-5-5`; change it with
`BGP_LLM_MODEL`). All of these settings live in `analyzer/llm_client.py`.

In agent mode the LLM is called once per step (a few seconds each); in rules
mode only when the rules and ML are stuck. `--no-llm` skips it (implies
`--mode rules`).

## Step 7 — Run the Analyze stage (healthy path)

```bash
python3 -m analyzer.run "My BGP peer is stuck at active state" \
  --host 172.20.20.2 --peer 172.20.20.3
```

With BGP up, the agent checks `bgp_state`, sees `Established` and concludes
(~20s with qwen2.5:7b):

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

`Triage` is the LLM's reading of the question before any tool ran; an
off-topic question stops there. `[rule: ...]` marks a result the rule book
decided on its own. `Rules agree: NO` means the LLM overrode a rule, so check
its steps. Wording and tool order vary between runs.

With `--mode rules`:

```
Resolved:      True
Root cause:    BGP peer 172.20.20.3 is Established
Suggested fix: No action needed — the session is up.
Decided by:    rules
Tools checked: bgp_state
Fault class:   healthy
ML opinion:    healthy (1.00)
```

If Ollama isn't running, agent mode falls back to the rule chain and says so
in a `Note:` line.

Every run also writes a log file to `logs/run_<date>-<time>.log` (the path is
printed as the last line, `Log file:`). It records each step in order: each
agent decision, every tool call with its payload, result and raw output, each
rule finding, the ML prediction, the LLM calls and their token usage, and the
final verdict. If a run crashes, the traceback is in the log too. Use
`--log-dir <dir>` to write logs somewhere else.

The first run trains the ML model (about a second) and saves it to
`analyzer/models/`. Later runs reuse it.

## Step 8 — Inject a fault the rules miss (neighbor shut down)

Shut the neighbor down on router1:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 shutdown" -c "end"
```

Run the analyzer again:

```bash
python3 -m analyzer.run "My BGP peer is stuck at active state" \
  --host 172.20.20.2 --peer 172.20.20.3
```

The agent sees `Idle (Admin)` (not `Active` as the question says) and
concludes `neighbor_shutdown`, sometimes after checking `config`.

With `--mode rules` it walks the whole chain (`bgp_state → interface → tcp_port → config`). None
of the rules fire: the interfaces are up, port 179 is reachable, and there's
no config baseline to diff against. The ML engine then picks up the
`Idle (Admin)` state and the `neighbor ... shutdown` line in the running
config:

```
Resolved:      True
Root cause:    Neighbor 172.20.20.3 is administratively shut down
Suggested fix: router bgp <local-asn> / no neighbor 172.20.20.3 shutdown
Decided by:    ml (confidence 1.00)
Tools checked: bgp_state -> interface -> tcp_port -> config
Fault class:   neighbor_shutdown
```

Run it with `--no-llm --no-ml` to compare: the rules alone report
"No root cause found by the rule chain."

Undo the fault:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "no neighbor 172.20.20.3 shutdown" -c "end"
```

## Step 9 — Inject an ambiguous fault (remote-as mismatch)

Point router1 at the wrong remote AS. The session keeps failing during the
OPEN exchange, which the ML engine can't pin down confidently without a
baseline:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65009" -c "end"
```

Wait ~10s, then run the analyzer:

```bash
python3 -m analyzer.run "My BGP peer won't come up" \
  --host 172.20.20.2 --peer 172.20.20.3
```

The agent sees `Idle`, then `bgp_neighbor` shows router1 sent a NOTIFICATION
(Bad Peer AS) and reads the AS router2 really uses from its OPEN:

```
Root cause:    This router expects AS 65009 for 172.20.20.3, but the peer uses AS 65002.
Suggested fix: router bgp 65001 neighbor 172.20.20.3 remote-as 65002
Tools checked: bgp_state -> bgp_neighbor
```

The same tool catches a neighbor removed on router2 (`Notification received
(Cease/Peer De-configured)`), which the other four tools can't see.

With `--mode rules`: when the ML engine's confidence is below 0.7 (or its answer is `unknown`), the
case goes to the LLM. Expect `Decided by: llm` with its root cause and fix,
plus a `Next checks:` list of commands to confirm it, and a
`Diagnosed by qwen2.5:7b` note. If Ollama isn't running you'll see
`Note: LLM escalation skipped: could not reach Ollama ...` instead.

The exact result depends on the BGP state at that moment. If the ML engine is
confident, it decides and the LLM isn't called. A small local model can be
wrong, so check its answer against `Next checks`.

Undo the fault:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"
```

<!-- Future Work -->
## Step 10 — (Optional) Save a baseline so config drift is caught by rules

The config rule only fires when a known-good baseline exists. With BGP healthy, save one per router:

```bash
python3 -c "
from tools.config import ConfigTool
t = ConfigTool()
for host in ['172.20.20.2', '172.20.20.3']:
    t.save_baseline(host, t.device_client.run_vtysh(host, 'show running-config').output)
"
```

Repeat the Step 9 fault: now the rules report
"Running config has drifted from the baseline", and the ML opinion names it
more specifically as `remote_as_mismatch`.

## Step 11 — (Optional) Retrain the ML model

The model starts out trained on synthetic cases. To retrain, and to add real labelled cases (one JSON object per line:
`{"evidence": [...], "peer": "172.20.20.3", "label": "remote_as_mismatch"}`):

```bash
python3 -m analyzer.ml_engine train                    # synthetic only
python3 -m analyzer.ml_engine train --cases cases.jsonl
```

It prints per-class precision/recall on a held-out split, then saves the model.
If the feature list in `ml_engine.py` changes, an old saved model is retrained
automatically on the next run.

---

## Troubleshooting

- **`Address already in use`** — an old API is still running, often in another
  terminal tab. Stop it with Ctrl-C in that tab, or from anywhere with
  `pkill -f "uvicorn api.main:app"` (`ss -ltnp | grep :8000` shows what holds
  the port). Or run on `--port 8001` and pass `--url http://localhost:8001` to
  the analyzer.
- **`Unable to connect to port 22`** — the lab isn't deployed, or you're using
  the wrong IP; re-check with `containerlab inspect`.
- **`% Can't open configuration file /etc/frr/vtysh.conf`** — harmless warning;
  the command still ran. The current Dockerfile creates this file, so rebuild
  the image (`InitialSetup.md`) and redeploy (Step 2) to get rid of it.
- **`WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!`** on `ssh admin@172.20.20.x`
  — expected after rebuilding the image, which generates new SSH host keys.
  Clear the old keys with
  `ssh-keygen -R 172.20.20.2 && ssh-keygen -R 172.20.20.3`. To skip this for the
  lab subnet only, add to `~/.ssh/config` in the VM:
  `Host 172.20.20.*` / `StrictHostKeyChecking no` / `UserKnownHostsFile /dev/null`.
  The analyzer isn't affected (paramiko doesn't read `known_hosts`).
- **`% Can not configure the local system as neighbor`** — the neighbor IP is
  the router's own address, which happens when the management IPs swapped on a
  deploy. Check with `containerlab inspect`. The pinned `mgmt-ipv4` in the
  topology prevents this; destroy and redeploy if the lab predates it.
- **`Root cause: The diagnostic tools could not reach the router`** — every tool
  failed over SSH; same fixes as `Unable to connect to port 22` above.
- **ML results look off after upgrading scikit-learn** — retrain with
  `python3 -m analyzer.ml_engine train`.
- **First agent answer takes minutes** — Ollama unloads the model after 5
  idle minutes and reloads it on the next call. Later runs are faster.

## Notes

- Always run the API and the analyzer from inside the VM and from the repo root.
- Management IPs are pinned in `topology.clab.yml`. If you remove the
  `mgmt-ipv4` lines, IPs follow container start order and can swap between
  deploys.
- The ML model is trained on synthetic data for now, so its confidence scores
  are indicative. The LLM treats its guess as a hint, not a fact.
- The classic "interface down → BGP Active" demo needs peering over the `eth1`
  data link (currently peering is over the management network).