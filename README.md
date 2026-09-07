# Real-Time AI Network Troubleshooter for BGP Switches

<!--
Suggested repo structure

real-time-ai-network-troubleshooter/
│
├── .github/
│   └── workflows/
│       └── ci.yml                  # CI/CD pipeline automation
│
├── docs/
│   ├── architecture.md             # System architecture and workflow details
│   ├── prerequisites.md            # Study notes on BGP, Netmiko, TextFSM, etc.
│   └── program_plan.md             # CPP-2 high-level timeline and milestones
│
├── lab/
│   ├── containerlab/               # Containerlab topologies (FRRouting, etc.)
│   └── scripts/                    # Quick experimentation scripts for switches
│
├── src/
│   ├── __init__.py
│   ├── collector/                  # SSH/API connection and command execution
│   │   ├── __init__.py
│   │   ├── ssh_client.py
│   │   └── api_client.py
│   ├── parser/                     # CLI output parsing templates & logic
│   │   ├── __init__.py
│   │   └── textfsm_parser.py
│   ├── analyzer/                   # ML engine and rule-based diagnostics
│   │   ├── __init__.py
│   │   └── ml_engine.py
│   └── dashboard/                  # Flask or Streamlit UI code
│       ├── __init__.py
│       └── app.py
│
├── tests/                          # Unit and integration tests
│   ├── __init__.py
│   ├── test_collector.py
│   └── test_parser.py
│
├── notebooks/                      # Jupyter notebooks for data exploration & ML training
│   └── bgp_data_exploration.ipynb
│
├── .gitignore
├── README.md                       # Project title, badges, setup instructions
├── requirements.txt                # Python dependencies (Netmiko, Pandas, Streamlit, etc.)
└── setup.py                        # Package installation setup
-->

