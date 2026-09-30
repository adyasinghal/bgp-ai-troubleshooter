"""
Tool cohort REST service.

Exposes each tool at the endpoint recorded in the Rules DB (`tools.endpoint`),
so the Orchestrator's Reasoning loop can call tools purely by REST path
without knowing SSH/vtysh details. This is the "REST-wrapped CLI tools" layer
from the architecture diagram, sitting on top of the "REST API layer /
Network devices" boundary (device_client.py).
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from tools.bgp_state import BGPStateTool
from tools.interface import InterfaceTool
from tools.tcp_port import TCPPortTool
from tools.config import ConfigTool
from rules_db.rules_db import RulesDB

app = FastAPI(title="Tool Cohort API", version="0.1.0")

rules_db = RulesDB()
bgp_tool = BGPStateTool()
interface_tool = InterfaceTool()
tcp_tool = TCPPortTool()
config_tool = ConfigTool()


class BGPStateRequest(BaseModel):
    host: str
    peer: str | None = None


class InterfaceRequest(BaseModel):
    host: str
    interface: str | None = None


class TCPCheckRequest(BaseModel):
    host: str
    peer_ip: str
    port: int = 179


class ConfigDiffRequest(BaseModel):
    host: str


@app.post("/tools/bgp/state")
def bgp_state(req: BGPStateRequest):
    result = bgp_tool.run(req.host, req.peer)
    return result.to_dict()


@app.post("/tools/interface/detail")
def interface_detail(req: InterfaceRequest):
    result = interface_tool.run(req.host, req.interface)
    return result.to_dict()


@app.post("/tools/tcp/check")
def tcp_check(req: TCPCheckRequest):
    result = tcp_tool.run(req.host, req.peer_ip, req.port)
    return result.to_dict()


@app.post("/tools/config/diff")
def config_diff(req: ConfigDiffRequest):
    result = config_tool.run(req.host)
    return result.to_dict()


# --- Rules DB read endpoints, so the Orchestrator can query intent -> tool mappings ---

@app.get("/rules/intents")
def list_intents():
    return {"intents": rules_db.list_intents()}


@app.get("/rules/tools")
def list_tools():
    return {"tools": [t.__dict__ for t in rules_db.list_tools()]}


# Declared before /rules/{intent} so "catalog" isn't taken as an intent name.
@app.get("/rules/catalog")
def catalog():
    """Every tool (with its args and usage notes) and every rule, in one call for the LLM agent."""
    return {
        "tools": [t.__dict__ for t in rules_db.list_tools()],
        "rules": [r.__dict__ for r in rules_db.list_rules()],
    }


@app.get("/rules/{intent}")
def get_rule_for_intent(intent: str):
    tools = rules_db.get_tools_for_intent(intent)
    if not tools:
        raise HTTPException(status_code=404, detail=f"No rule found for intent '{intent}'")
    next_intent = rules_db.get_next_intent_on_fail(intent)
    return {
        "intent": intent,
        "tools": [t.__dict__ for t in tools],
        "next_intent_on_fail": next_intent,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
