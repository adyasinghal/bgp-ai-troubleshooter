# Real-Time AI Network Troubleshooter for BGP Switches

A hybrid network troubleshooting system for BGP switches and routers combining deterministic forward-chaining rules with an LLM-assisted explanation layer (Google Gemini).

---

## 📖 Documentation & Guides

All detailed setup, usage, and architectural guides are located in the [`docs/`](docs/) directory:

- 🚀 **[Guide to Run & Test (docs/GuideToRun.md)](docs/GuideToRun.md)** — Complete step-by-step instructions to start the API, run diagnostic CLI commands (`--llm`), execute test suites, and simulate live Containerlab network faults.
- ⚙️ **[Initial Setup Guide (docs/InitialSetup.md)](docs/InitialSetup.md)** — Environment setup for OrbStack, Docker, Containerlab, and custom FRR router container image building.
- 🧠 **[Tool Design & Architecture (docs/ToolDesign.md)](docs/ToolDesign.md)** — Detailed specification of auto-triage, tool cohort, deterministic reasoning rules, and AI explanation pipeline.
- 📚 **[BGP Pre-requisites & Study Notes (docs/Pre-requisites.md)](docs/Pre-requisites.md)** — Networking fundamentals, BGP finite state machine, and peering concepts.
- 📋 **[Project Overview (docs/project_overview.md)](docs/project_overview.md)** — HPE CPP-2 program roadmap, objectives, and high-level deliverables.

---

## 📂 Suggested Repo Structure

```
bgp-ai-troubleshooter/              
├── docs/  
│   ├── GuideToRun.md                       # Instructions on how to run and test the tool  
│   ├── InitialSetup.md                     # Setup Instructions         
│   ├── Pre-requisites.md                   # Study notes on BGP   
│   ├── project_overview.md                 # Problem statement & program roadmap
│   └── ToolDesign.md                       # Auto triage, tools, and rules design                   
├── lab/  
│   └── containerlab/                       # Containerlab topologies (FRRouting, etc.)  
│       ├── Dockerfile              
│       └── topology.clab.yml       
├── tools/                          
│   ├── __init__.py
│   ├── base_tool.py
│   ├── bgp_state.py
│   ├── config.py
│   ├── device_client.py
│   ├── interface.py
│   └── tcp_port.py
├── rules_db/                       
│   ├── __init__.py
│   ├── rules_db.py
│   └── schema.sql
├── api/                                    # Tool Cohort REST API  
│   ├── __init__.py
│   └── main.py  
├── analyzer/                               # Deterministic Reasoning & AI Diagnosis  
│   ├── __init__.py  
│   ├── triage.py                           # Question → starting intent  
│   ├── rules_engine.py                     # Deterministic reasoning loop  
│   ├── verdict.py                          # Authoritative root cause & audit trail  
│   ├── llm_engine.py                       # LLM explanation layer (Gemini)  
│   ├── rest_client.py                      # Calls Tool API over HTTP  
│   └── run.py                              # CLI runner (`--llm`)  
├── dashboard/                              # Optional UI code  
│   └── __init__.py  
├── tests/  
│   ├── __init__.py  
│   ├── test_analyzer.py  
│   ├── test_api.py  
│   ├── test_llm_engine.py  
│   ├── test_tools.py  
│   └── test_triage.py  
├── demo_llm_diagnosis.py                   # Demonstration & fallback script
├── .gitignore  
├── requirements.txt                        # Python dependencies  
└── README.md
```
