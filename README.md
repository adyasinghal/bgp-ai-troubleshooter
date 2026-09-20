# Real-Time AI Network Troubleshooter for BGP Switches

<!--
Suggested repo structure


bgp-ai-troubleshooter/            
├── docs/
│   ├── project_overview.md       
│   ├── InitialSetup.md                     # Setup Instructions       
│   ├── Pre-requisites.md                   # Study notes on BGP         
├── lab/
│   └── containerlab/                       # Containerlab topologies (FRRouting, etc.)
│       ├── Dockerfile            
│       └── topology.clab.yml     
├── tools/                        
│   ├── __init__.py  base_tool.py  device_client.py
│   ├── bgp_state.py  interface.py  tcp_port.py  config.py
├── rules_db/                     
│   ├── __init__.py  rules_db.py  schema.sql
├── api/                                    # SSH/API connection and command execution
│   ├── __init__.py  main.py
├── analyzer/                               # ML engine and rule-based diagnostics
│   ├── __init__.py
│   ├── rest_client.py            (calls API over HTTP)
│   ├── triage.py                 (question → starting intent)
│   ├── rules_engine.py           (the reasoning loop)
│   ├── verdict.py                (builds root_cause/fix output)
│   └── ml_engine.py              (scikit-learn)
├── dashboard/                              # Flask or Streamlit UI code
│   └── __init__.py
├── tests/
│   ├── __init__.py  test_tools.py  test_analyzer.py
├── .gitignore
├── requirements.txt                        # Python dependencies
├── README.md
└── setup.py                                # Package installation setup
-->

