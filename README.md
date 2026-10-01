# Real-Time AI Network Troubleshooter for BGP Switches

When a BGP session between two routers goes down, finding out why usually means an engineer logging in, running a series of `show` commands and reading through the output. This project is an AI troubleshooter that does that work: you describe the problem in plain language, it investigates the switch itself and tells you the root cause and how to fix it.

The aim is a tool an engineer can attach to a misbehaving BGP device and simply follow its recommendation. It's being built in phases:

1. **Configuration faults** (current): diagnose misconfigurations such as a shut down neighbor, a wrong remote AS, a down interface or a blocked BGP port.
2. **Log-based detection**: find anomalies from event and support logs.
3. **Real-time monitoring**: collect data periodically and suggest fixes as problems appear, with a dashboard for alerts.

It combines deterministic rules, an ML classifier and an LLM, so it stays accurate on known faults while still being able to reason about new ones. It's developed and tested on FRRouting routers in a Containerlab lab.

Built as part of the HPE CPP-2 program.

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
│   ├── route_table.py  route_map.py  
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


