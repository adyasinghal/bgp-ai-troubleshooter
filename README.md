# Real-Time AI Network Troubleshooter for BGP Switches

Suggested repo structure  

```
bgp-ai-troubleshooter/              
├── docs/  
│   ├── GuideToRun.md                       # Instructions on how to run and test the tool  
│   ├── InitialSetup.md                     # Setup Instructions         
│   ├── Pre-requisites.md                   # Study notes on BGP   
│   ├── project_overview.md         
│   ├── ToolDesign.md                       # States working of auto triage, tools and rules                   
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


