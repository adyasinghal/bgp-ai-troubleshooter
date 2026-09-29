# How to Run & Test the BGP AI Troubleshooter

This guide takes you from a fresh machine to running the full workflow:
lab → REST tools → Analyze stage.

The Analyze stage escalates in this order, stopping as soon as one step finds a root cause:

```
rule chain → ML engine (scikit-learn) → LLM (local model via Ollama) → human
```

Everything runs **inside the OrbStack `clab` VM**, because the tool reaches the routers over SSH and those routers are only reachable from inside the VM. The one exception is Ollama, which runs on the Mac (see Step 7).

## Prerequisites (one-time)

- OrbStack installed, with an Ubuntu VM named `clab` (see `InitialSetup.md`).
- Docker + Containerlab installed inside that VM.
- Python 3 + pip inside the VM.
- *(Optional)* Ollama on the Mac, for the LLM escalation step (free, runs locally; Step 7). Without it everything else still works and the verdict says the LLM step was skipped.

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

## Step 2 — Build the router image (once; rebuild only if the Dockerfile changes)

```bash
cd lab/containerlab
docker build -t frr-ssh:8.5.2 .
cd ../..
```

If the lab is already running, a rebuild doesn't reach the existing containers, and `deploy` just reports `no changes`. Destroy the lab first so Step 3 recreates the containers from the new image:

```bash
sudo containerlab destroy -t lab/containerlab/topology.clab.yml
```

## Step 3 — Deploy the lab

```bash
sudo containerlab deploy -t lab/containerlab/topology.clab.yml
```

The topology pins the management IPs (`mgmt-ipv4`), so every deploy gives **router1 = 172.20.20.2** and **router2 = 172.20.20.3**, the addresses this guide uses.  
Check them anytime with:

```bash
sudo containerlab inspect -t lab/containerlab/topology.clab.yml
```

## Step 4 — Configure the BGP neighbors

(Neighbor config is not saved across a redeploy; bgpd is already enabled in the image.)

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"
docker exec clab-bgp-lab-router2 vtysh -c "conf t" -c "router bgp 65002" -c "neighbor 172.20.20.2 remote-as 65001" -c "end"
```

Confirm it's up (wait ~10s for `Established`):

```bash
ssh admin@172.20.20.2 'vtysh -c "show bgp summary"'   # password: admin
```

## Step 5 — Start the REST API (Terminal 1)

Run this from the repo root (`bgp-ai-troubleshooter/`), not from `lab/`:

```bash
python3 -m pip install -r requirements.txt --break-system-packages   # first time, and after requirements.txt changes
python3 -m uvicorn api.main:app --host 0.0.0.0 --port 8000
```

Leave this running. You should see `Application startup complete.`

## Step 6 — Smoke-test the API (Terminal 2)

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

## Step 7 — (Optional) Enable LLM escalation with Ollama

The LLM step uses a free local model served by Ollama. Ollama runs **on the Mac, not in the VM**, so it can use the Apple GPU. The analyzer in the VM reaches it at `http://host.orb.internal:11434`, which OrbStack forwards to the Mac's localhost.

**7a. On the Mac (a normal macOS terminal, not `orb -m clab`), once:**

```bash
brew install ollama
brew services start ollama          # runs the Ollama server now and at login
ollama pull qwen2.5:7b              # ~4.7 GB download; the default model
ollama run qwen2.5:7b "say hi"      # quick check; the first load takes a few seconds
```

(Or install the Ollama app from ollama.com and open it instead of `brew services start`.)

**7b. In the VM (Terminal 2), check the VM can reach it:**

```bash
curl http://host.orb.internal:11434/api/tags   # should list qwen2.5:7b
```

**7c. (Optional) Settings.** The defaults work as-is; set these only to change them:

```bash
export BGP_LLM_MODEL=qwen2.5:7b                      # any model you've pulled
export BGP_LLM_URL=http://host.orb.internal:11434    # where Ollama is
export BGP_LLM_NUM_CTX=16384                         # context window (tokens)
export BGP_LLM_TIMEOUT=300                           # seconds to wait for an answer
```

The LLM is only called when both the rules and the ML engine are stuck. It's free, and nothing leaves your machine. Add `--no-llm` to any analyzer run to skip it. 

## Step 8 — Run the Analyze stage (healthy path)

```bash
python3 -m analyzer.run "My BGP peer is stuck at active state" \
  --host 172.20.20.2 --peer 172.20.20.3
```

With BGP up, it should check `bgp_state`, find `Established`, and report
"no action needed":

```
Resolved:      True
Root cause:    BGP peer 172.20.20.3 is Established
Suggested fix: No action needed — the session is up.
Decided by:    rules
Tools checked: bgp_state
ML opinion:    healthy (1.00)
```

`ML opinion` is the ML engine's second opinion; it's shown whenever the rules
made the decision.

Every run also writes a log file to `logs/run_<date>-<time>.log` (the path is
printed as the last line, `Log file:`). It records each step in order: the
starting intent, each rule fetched, every tool call with its payload, result
and raw output, each rule decision and escalation, the ML prediction with its
probabilities, the LLM call and its token usage, and the final verdict. If a
run crashes, the traceback is in the log too. Use `--log-dir <dir>` to write
logs somewhere else.

The first run trains the ML model (about a second) and saves it to
`analyzer/models/`. Later runs reuse it.

## Step 9 — Test the ML engine (inject a fault the rules miss)

Shut the neighbor down on router1:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 shutdown" -c "end"
```

Run the analyzer again:

```bash
python3 -m analyzer.run "My BGP peer is stuck at active state" \
  --host 172.20.20.2 --peer 172.20.20.3
```

It walks the whole chain (`bgp_state → interface → tcp_port → config`). None
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
```

Run it with `--no-ml` to compare: the rules alone report
"No root cause found by the rule chain."

Undo the fault:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "no neighbor 172.20.20.3 shutdown" -c "end"
```

## Step 10 — Test the LLM escalation (inject an ambiguous fault)

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

When the ML engine's confidence is below 0.7 (or its answer is `unknown`), the
case goes to the LLM. Expect `Decided by: llm` with its root cause and fix,
plus a `Next checks:` list of commands to confirm it, and a
`Diagnosed by qwen2.5:7b` note. If Ollama isn't running you'll see
`Note: LLM escalation skipped: could not reach Ollama ...` instead.

The exact result depends on the BGP state at that moment. If the ML engine is
confident, it decides and the LLM isn't called. A small local model is less
reliable than Claude, so always check its answer against `Next checks`.

Undo the fault:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"
```

<!-- Future Work -->
## Step 11 — (Optional) Save a baseline so config drift is caught by rules

The config rule only fires when a known-good baseline exists. With BGP healthy, save one per router:

```bash
python3 -c "
from tools.config import ConfigTool
t = ConfigTool()
for host in ['172.20.20.2', '172.20.20.3']:
    t.save_baseline(host, t.device_client.run_vtysh(host, 'show running-config').output)
"
```

Repeat the Step 10 fault: now the rules report
"Running config has drifted from the baseline", and the ML opinion names it
more specifically as `remote_as_mismatch`.

## Step 12 — (Optional) Retrain the ML model

The model starts out trained on synthetic cases. To retrain, and to add real labelled cases (one JSON object per line:
`{"evidence": [...], "peer": "172.20.20.3", "label": "remote_as_mismatch"}`):

```bash
python3 -m analyzer.ml_engine train                    # synthetic only
python3 -m analyzer.ml_engine train --cases cases.jsonl
```

It prints per-class precision/recall on a held-out split, then saves the model.

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
  the image (Step 2) and redeploy (Step 3) to get rid of it.
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

## Notes

- Always run the API and the analyzer from inside the VM and from the repo root.
- Management IPs are pinned in `topology.clab.yml`. If you remove the
  `mgmt-ipv4` lines, IPs follow container start order and can swap between
  deploys.
- The ML model is trained on synthetic data for now, so its confidence scores
  are indicative. Claude treats its guess as a hint, not a fact.
- The classic "interface down → BGP Active" demo needs peering over the `eth1`
  data link (currently peering is over the management network).