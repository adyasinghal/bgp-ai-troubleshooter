# How to Run & Test the BGP AI Troubleshooter

This guide takes you from a fresh machine to running the full workflow:
lab → REST tools → Analyze stage.

The Analyze stage escalates in this order, stopping as soon as one step finds a root cause:

```
rule chain → ML engine (scikit-learn) → LLM (Claude) → human
```

Everything runs **inside the OrbStack `clab` VM**, because the tool reaches the routers over SSH and those routers are only reachable from inside the VM.

## Prerequisites (one-time)

- OrbStack installed, with an Ubuntu VM named `clab` (see `InitialSetup.md`).
- Docker + Containerlab installed inside that VM.
- Python 3 + pip inside the VM.
- *(Optional)* An Anthropic API key, for the LLM escalation step. Without it
  everything else still works and the verdict says the LLM step was skipped.

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

If the lab is already running, a rebuild doesn't reach the existing containers,
and `deploy` just reports `no changes`. Destroy the lab first so Step 3
recreates the containers from the new image:

```bash
sudo containerlab destroy -t lab/containerlab/topology.clab.yml
```

## Step 3 — Deploy the lab

```bash
sudo containerlab deploy -t lab/containerlab/topology.clab.yml
```

The topology pins the management IPs (`mgmt-ipv4`), so every deploy gives
**router1 = 172.20.20.2** and **router2 = 172.20.20.3**, the addresses this guide uses.
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

## Step 7 — (Optional) Enable LLM escalation (Terminal 2)

```bash
export ANTHROPIC_API_KEY=sk-ant-...        # your key; add to ~/.bashrc to keep it
export BGP_LLM_MODEL=claude-opus-5         # optional; this is the default
```

Claude is only called when both the rules and the ML engine are stuck, and each
call is billed to that key. Add `--no-llm` to any analyzer run to skip it.

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
case goes to Claude. Expect `Decided by: llm` with Claude's root cause and fix,
plus a `Next checks:` list of commands to confirm it. Without an API key you'll
see `Note: LLM escalation skipped: no Anthropic credentials` instead.

The exact result depends on the BGP state at that moment. If the ML engine is
confident, it decides and Claude isn't called.

Undo the fault:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 remote-as 65002" -c "end"
```

<!-- Future Work -->
## Step 11 — (Optional) Save a baseline so config drift is caught by rules

The config rule only fires when a known-good baseline exists. With BGP healthy,
save one per router:

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

The model starts out trained on synthetic cases. To retrain, and to add real
labelled cases (one JSON object per line:
`{"evidence": [...], "peer": "172.20.20.3", "label": "remote_as_mismatch"}`):

```bash
python3 -m analyzer.ml_engine train                    # synthetic only
python3 -m analyzer.ml_engine train --cases cases.jsonl
```

It prints per-class precision/recall on a held-out split, then saves the model.

---

## Troubleshooting

- **`ModuleNotFoundError: No module named 'tools'/'analyzer'`** — run from the
  repo root, not from inside a sub-folder.
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
- **`ModuleNotFoundError: No module named 'sklearn'` / `'anthropic'`** — re-run
  the `pip install -r requirements.txt` line from Step 5.
- **`LLM escalation skipped: no Anthropic credentials`** — `ANTHROPIC_API_KEY`
  isn't set in *this* terminal (Step 7). Other `skipped` reasons (rate limit,
  connection error) are printed the same way.
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