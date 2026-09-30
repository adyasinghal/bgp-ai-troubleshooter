# Real-Time AI Network Troubleshooter for BGP Switches

Suggested repo structure  

```
bgp-ai-troubleshooter/              
├── docs/  
│   ├── GuideToRun.md                       # Instructions on how to run and test the tool  
│   ├── InitialSetup.md                     # One-time setup: VM, lab image, Python, Ollama   
│   ├── Pre-requisites.md                   # Study notes (TCP/IP basics)   
│   ├── project_overview.md         
│   ├── ToolDesign.md                       # States working of auto triage, tools and rules                   
├── lab/  
│   └── containerlab/                       # Containerlab topologies (FRRouting, etc.)  
│       ├── Dockerfile              
│       └── topology.clab.yml       
├── tools/                          
│   ├── \_\_init\_\_.py  base_tool.py  device_client.py  
│   ├── bgp_state.py  bgp_neighbor.py  interface.py  tcp_port.py  config.py  
├── rules_db/                       
│   ├── \_\_init\_\_.py  rules_db.py  schema.sql  
├── api/                                    # SSH/API connection and command execution  
│   ├── \_\_init\_\_.py  main.py  
├── analyzer/                               # ML engine and rule-based diagnostics  
│   ├── \_\_init\_\_.py  
│   ├── agent.py                  (LLM agent: picks each tool, reads results, concludes)  
│   ├── prompts.py                (agent prompt and decision schemas)  
│   ├── llm_client.py             (one entry point for LLM calls: Ollama or Claude)  
│   ├── rest_client.py            (calls API over HTTP)  
│   ├── triage.py                 (reads the question: LLM, or keywords in rules mode)  
│   ├── rules_engine.py           (rule findings, and the rules-mode chain)  
│   ├── tool_registry.py          (tool catalog: validates and runs tool calls)  
│   ├── verdict.py                (builds root_cause/fix output)  
│   ├── ml_engine.py              (scikit-learn fault classifier)  
│   └── llm_escalation.py         (rules mode: hands unresolved cases to the LLM)  
├── dashboard/                              # Flask or Streamlit UI code  
│   └── \_\_init\_\_.py  
├── .gitignore  
├── requirements.txt                        # Python dependencies  
├── README.md  
└── setup.py                                # Package installation setup  
```


