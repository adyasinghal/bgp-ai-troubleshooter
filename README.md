# Real-Time AI Network Troubleshooter for BGP Switches

Suggested repo structure  

```
bgp-ai-troubleshooter/              
├── docs/  
│   ├── GuideToRun.md                       # Instructions on how to run and test the tool  
│   ├── InitialSetup.md                     # Setup Instructions         
│   ├── Pre-requisites.md                   # Study notes on BGP   
│   ├── project_overview.md         
│   └── ToolDesign.md                       # Auto triage, tools, and rules design                   
├── lab/  
│   └── containerlab/                       # Containerlab topologies (FRRouting, etc.)  
│       ├── Dockerfile              
│       └── topology.clab.yml       
├── tools/                          
│   ├── __init__.py  base_tool.py  device_client.py  
│   └── bgp_state.py  interface.py  tcp_port.py  config.py  
├── rules_db/                       
│   └── __init__.py  rules_db.py  schema.sql  
├── api/                                    # Tool Cohort REST API  
│   └── __init__.py  main.py  
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
│   ├── test_tools.py  
│   ├── test_analyzer.py  
│   ├── test_triage.py  
│   ├── test_api.py  
│   └── test_llm_engine.py  
├── demo_llm_diagnosis.py                   # Demonstration & fallback script
├── .gitignore  
├── requirements.txt                        # Python dependencies  
└── README.md
```
