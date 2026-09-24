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
│   ├── \_\_init\_\_.py  base_tool.py  device_client.py  
│   ├── bgp_state.py  interface.py  tcp_port.py  config.py 
├── rules_db/                       
│   ├── \_\_init\_\_.py  rules_db.py  schema.sql  
├── api/                                    # SSH/API connection and command execution  
│   ├── \_\_init\_\_.py  main.py  
├── analyzer/                               # ML engine and rule-based diagnostics  
│   ├── \_\_init\_\_.py  
│   ├── rest_client.py            (calls API over HTTP)  
│   ├── triage.py                 (question → starting intent)  
│   ├── rules_engine.py           (the reasoning loop)  
│   ├── verdict.py                (builds root_cause/fix output)  
│   ├── ml_engine.py              (scikit-learn fault classifier)  
│   └── llm_escalation.py         (hands unresolved cases to Claude)  
├── dashboard/                              # Flask or Streamlit UI code  
│   └── \_\_init\_\_.py  
├── tests/  
│   ├── \_\_init\_\_.py  test_tools.py  test_analyzer.py  
├── .gitignore  
├── requirements.txt                        # Python dependencies  
├── README.md  
└── setup.py                                # Package installation setup  
```
