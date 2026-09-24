# How to Run & Test the BGP AI Troubleshooter

This guide takes you from a fresh machine to running the full workflow:
lab → REST tools → Analyze stage.

Everything runs **inside the OrbStack `clab` VM**, because the tool reaches the routers over SSH and those routers are only reachable from inside the VM.

## Prerequisites (one-time)

- OrbStack installed, with an Ubuntu VM named `clab` (see `InitialSetup.md`).
- Docker + Containerlab installed inside that VM.
- Python 3 + pip inside the VM.

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
cd lab
docker build -t frr-ssh:8.5.2 .
cd ..
```

## Step 3 — Deploy the lab

```bash
sudo containerlab deploy -t lab/topology.clab.yml
```

Note the management IPs from the printed table — they can differ each deploy.
This guide assumes **router1 = 172.20.20.2**, **router2 = 172.20.20.3**.
Re-check anytime with:

```bash
sudo containerlab inspect -t lab/topology.clab.yml
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

```bash
python3 -m pip install -r requirements.txt --break-system-packages   # first time only
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

## Step 6.5 — Run Automated Unit & Reasoning Tests

Run the offline automated test suite (does not require live routers or SSH):

```bash
pytest -v
```

This verifies:
- TCP reachability parsing and normalization (no false substring matches)
- Config baseline safety (missing baseline does not falsely report drift)
- Interface diagnosis relevance based on peer IP subnet matching
- Deterministic triage keyword classification for all starting points
- Forward reasoning escalation chain (all 10 troubleshooting paths)
- Enhanced Verdict presentation and evidence extraction
- REST API endpoint contracts and error resilience

## Step 7 — Run the Analyze stage (healthy path)


```bash
python3 -m analyzer.run "My BGP peer is stuck at active state" \
  --host 172.20.20.2 --peer 172.20.20.3
```

With BGP up, it should check `bgp_state`, find `Established`, and report
"no action needed."

## Step 8 — Test the escalation (inject a fault)

Break the session:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.3 shutdown" -c "end"
```

Run the analyzer again — now it walks the chain (`bgp_state → interface → tcp_port → config`) and reports a root cause:

```bash
python3 -m analyzer.run "My BGP peer is stuck at active state" \
  --host 172.20.20.2 --peer 172.20.20.3
```

Undo the fault when done:

```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "no neighbor 172.20.20.3 shutdown" -c "end"
```

---

## Troubleshooting

- **`ModuleNotFoundError: No module named 'tools'/'analyzer'`** — run from the
  repo root, not from inside a sub-folder.
- **`Address already in use`** — an old API is still running; stop it (Ctrl-C)
  or use `--port 8001` (and update the curl/analyzer `--url`).
- **`Unable to connect to port 22`** — the lab isn't deployed, or you're using
  the wrong IP; re-check with `containerlab inspect`.
- **`% Can't open configuration file /etc/frr/vtysh.conf`** — harmless warning,
  ignore it.

## Notes

- Always run the API and the analyzer from inside the VM and from the repo root.
- IPs can change per deploy — substitute what the deploy table shows.
- The classic "interface down → BGP Active" demo needs peering over the `eth1`
  data link (currently peering is over the management network).