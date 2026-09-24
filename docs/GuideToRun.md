# How to Run & Test the BGP AI Troubleshooter

This guide takes you from a fresh machine to running the full workflow:
Containerlab environment → REST API Tool Cohort → Deterministic Reasoning & AI Diagnosis.

---

## 1. Prerequisites (One-Time Setup)

- **Containerlab & Docker**: Installed on your Linux host / VM (see [InitialSetup.md](InitialSetup.md)).
- **Python 3.10+** & `pip`.
- **Gemini API Key** *(optional for `--llm` diagnosis)*.

### Install Dependencies
From the repository root:
```bash
pip install -r requirements.txt google-genai python-dotenv pytest
```

### Configure Gemini AI (Optional for LLM Diagnosis)
Create a `.env` file in the project root:
```env
LLM_API_KEY=your_gemini_api_key_here
LLM_MODEL=gemini-3.5-flash-lite
LLM_ENABLED=true
```
*(If no API key is provided, the tool operates in deterministic rule-based mode or gracefully falls back).*

---

## 2. Running Automated Tests (Offline Verification)

You can run the full automated test suite anytime without needing live routers or SSH connections:

```bash
pytest -v
```

This tests:
- **`tests/test_tools.py`**: TCP reachability, BGP states, interface subnet detection, config baselines.
- **`tests/test_triage.py`**: Deterministic keyword triage to intent mappings.
- **`tests/test_analyzer.py`**: Escalation chains, loop prevention, and root cause determination.
- **`tests/test_api.py`**: Tool Cohort REST API endpoints and error resilience.
- **`tests/test_llm_engine.py`**: Evidence sanitization, Gemini API integration, JSON schema validation, and offline fallback.

---

## 3. Deploying the Live Containerlab Topology

### Step 3.1 — Build the Router Image (FRR + SSH)
*(Only needed once or when `lab/containerlab/Dockerfile` changes)*
```bash
cd lab/containerlab
docker build -t frr-ssh:8.5.2 .
cd ../..
```

### Step 3.2 — Deploy the Lab
```bash
sudo containerlab deploy -t lab/containerlab/topology.clab.yml
```

Inspect assigned management IPs anytime:
```bash
sudo containerlab inspect -t lab/containerlab/topology.clab.yml
```
*(Commonly `router1` = `172.20.20.3` or `172.20.20.2`, `router2` = `172.20.20.2` or `172.20.20.3`)*.

### Step 3.3 — Configure BGP Neighbors
```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.2 remote-as 65002" -c "end"
docker exec clab-bgp-lab-router2 vtysh -c "conf t" -c "router bgp 65002" -c "neighbor 172.20.20.3 remote-as 65001" -c "end"
```

Verify neighbor session (wait ~10 seconds for `Established`):
```bash
ssh admin@172.20.20.3 'vtysh -c "show bgp summary"'   # password: admin
```

---

## 4. Running the Troubleshooter

You will use two terminal sessions from the project root:

### Terminal 1: Start the Tool Cohort REST API
```bash
uvicorn api.main:app --host 0.0.0.0 --port 8000
```
Leave this running. You should see `Application startup complete.`

### Terminal 2: Run the Diagnostic Analyzer

#### Scenario A: Deterministic Rule-Based Diagnosis
```bash
python -m analyzer.run "Why is my BGP session down?" --host 172.20.20.3 --peer 172.20.20.2
```

#### Scenario B: Deterministic + AI-Assisted Explanation Layer (`--llm`)
```bash
python -m analyzer.run "Why is my BGP session down?" --host 172.20.20.3 --peer 172.20.20.2 --llm
```

---

## 5. Testing Fault Scenarios & Escalation

### Fault 1: Administratively Shutdown Neighbor
Simulate an operator error on `router1`:
```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "neighbor 172.20.20.2 shutdown" -c "end"
```

Run the troubleshooter:
```bash
python -m analyzer.run "Why is BGP down on router1?" --host 172.20.20.3 --peer 172.20.20.2 --llm
```
*Result*: The tool detects `Idle (Admin)` state, verifies transport is reachable, and the LLM produces a root-cause explanation and remediation steps (`no neighbor shutdown`).

Recover from fault:
```bash
docker exec clab-bgp-lab-router1 vtysh -c "conf t" -c "router bgp 65001" -c "no neighbor 172.20.20.2 shutdown" -c "end"
```

---

## 6. Standalone Fallback / Offline AI Demonstration

To test the LLM explanation engine and offline fallback directly:
```bash
python demo_llm_diagnosis.py
```

---

## 7. Troubleshooting Common Issues

| Issue | Cause | Solution |
| :--- | :--- | :--- |
| `ModuleNotFoundError: No module named 'tools'/'analyzer'` | Script executed from subfolder | Run commands from the repository root `bgp-ai-troubleshooter/`. |
| `Address already in use (port 8000)` | API process already running | Stop existing process or use `--port 8001` and pass `--url http://localhost:8001` to `analyzer.run`. |
| `Unable to connect to port 22` / `Connection refused` | Router not running or wrong IP | Run `sudo containerlab inspect -t lab/containerlab/topology.clab.yml` to check running container IPs. |
| `LLM fallback: No API key provided` | `.env` missing or empty `LLM_API_KEY` | Add your Gemini key to `.env` or run without `--llm` for deterministic diagnosis. |