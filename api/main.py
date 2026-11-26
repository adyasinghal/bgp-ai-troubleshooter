"""
Tool cohort REST service.

Exposes each tool at the endpoint recorded in the Rules DB (`tools.endpoint`),
so the Orchestrator's Reasoning loop can call tools purely by REST path
without knowing SSH/vtysh details. This is the "REST-wrapped CLI tools" layer
from the architecture diagram, sitting on top of the "REST API layer /
Network devices" boundary (device_client.py).
"""

from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tools.bgp_state import BGPStateTool
from tools.bgp_neighbor import BGPNeighborTool
from tools.interface import InterfaceTool
from tools.tcp_port import TCPPortTool
from tools.config import ConfigTool
from tools.route_table import RouteTableTool
from tools.route_map import RouteMapTool
from tools.route_to_peer import RouteToPeerTool
from tools.prefix_list import PrefixListTool
from rules_db.rules_db import RulesDB

app = FastAPI(title="Tool Cohort API", version="0.1.0")

rules_db = RulesDB()
bgp_tool = BGPStateTool()
neighbor_tool = BGPNeighborTool()
interface_tool = InterfaceTool()
tcp_tool = TCPPortTool()
config_tool = ConfigTool()
route_table_tool = RouteTableTool()
route_map_tool = RouteMapTool()
route_to_peer_tool = RouteToPeerTool()
prefix_list_tool = PrefixListTool()


class BGPStateRequest(BaseModel):
    host: str
    peer: str | None = None


class BGPNeighborRequest(BaseModel):
    host: str
    peer: str


class InterfaceRequest(BaseModel):
    host: str
    interface: str | None = None


class TCPCheckRequest(BaseModel):
    host: str
    peer_ip: str
    port: int = 179


class ConfigDiffRequest(BaseModel):
    host: str


class RouteTableRequest(BaseModel):
    host: str
    prefix: str | None = None


class RouteMapRequest(BaseModel):
    host: str
    name: str | None = None


class RouteToPeerRequest(BaseModel):
    host: str
    peer: str


class PrefixListRequest(BaseModel):
    host: str
    name: str | None = None
    prefix: str | None = None


@app.post("/tools/bgp/state")
def bgp_state(req: BGPStateRequest):
    result = bgp_tool.run(req.host, req.peer)
    return result.to_dict()


@app.post("/tools/bgp/neighbor")
def bgp_neighbor(req: BGPNeighborRequest):
    result = neighbor_tool.run(req.host, req.peer)
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


@app.post("/tools/route/table")
def route_table(req: RouteTableRequest):
    result = route_table_tool.run(req.host, req.prefix)
    return result.to_dict()


@app.post("/tools/route/map")
def route_map(req: RouteMapRequest):
    result = route_map_tool.run(req.host, req.name)
    return result.to_dict()


@app.post("/tools/route/peer")
def route_to_peer(req: RouteToPeerRequest):
    result = route_to_peer_tool.run(req.host, req.peer)
    return result.to_dict()


@app.post("/tools/route/prefix-list")
def prefix_list(req: PrefixListRequest):
    result = prefix_list_tool.run(req.host, req.name, req.prefix)
    return result.to_dict()


# --- Rules DB read endpoints, so the Orchestrator can query intent -> tool mappings ---

@app.get("/rules/intents")
def list_intents():
    return {"intents": rules_db.list_intents()}


@app.get("/rules/tools")
def list_tools():
    return {"tools": [t.__dict__ for t in rules_db.list_tools()]}


# must come before /rules/{intent}
@app.get("/rules/catalog")
def catalog():
    return {
        "tools": [t.__dict__ for t in rules_db.list_tools()],
        "rules": [r.__dict__ for r in rules_db.list_rules()],
    }


@app.get("/rules/{intent}")
def get_rule_for_intent(intent: str):
    tools = rules_db.get_tools_for_intent(intent)
    if not tools:
        raise HTTPException(status_code=404, detail=f"No rule found for intent '{intent}'")
    rule = rules_db.get_rule(intent)
    return {
        "intent": intent,
        "tools": [t.__dict__ for t in tools],
        "next_intent_on_fail": rule.next_intent_on_fail,
        "symptoms": rule.symptoms,
        "likely_causes": rule.likely_causes,
        "verification": rule.verification,
    }


# --- Engineer feedback on uncertain diagnoses (analyzer/review.py, analyzer/feedback.py) ---
# A run the analyzer wasn't confident about waits as "pending". A dashboard lists them,
# shows the diagnosis and why it was flagged, and sends the engineer's reply or rating.

class FeedbackRequest(BaseModel):
    message: Optional[str] = Field(None, max_length=2000,
                                   description="what's wrong, what to check, or how to fix it; "
                                               "the agent re-investigates with it")
    correct: Optional[bool] = Field(None, description="the engineer's verdict on the latest diagnosis")
    actual_fault_class: Optional[str] = Field(None, description="the real fault class, when correct is false")
    comment: Optional[str] = Field(None, max_length=2000)


def _run_or_404(run_id: str) -> dict:
    from analyzer import feedback
    try:
        return feedback.load(run_id)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/feedback/pending")
def feedback_pending():
    from analyzer import feedback
    return {"pending": feedback.pending()}


@app.get("/feedback/stats")
def feedback_stats():
    from analyzer import feedback
    return feedback.stats()


@app.get("/feedback/{run_id}")
def feedback_run(run_id: str):
    return _run_or_404(run_id)


@app.post("/feedback/{run_id}")
def feedback_reply(run_id: str, req: FeedbackRequest):
    """Send a reply (re-investigates and returns the new diagnosis) or a rating, not both."""
    from analyzer import review
    from analyzer.rest_client import RestClient
    record = _run_or_404(run_id)
    if (req.message is None) == (req.correct is None):
        raise HTTPException(status_code=422, detail="send either message or correct")
    try:
        if req.correct is not None:
            return {"run_id": run_id, "operator": review.rate(record, req.correct,
                                                              req.actual_fault_class, req.comment)}
        # The agent calls this same API for its tools: this endpoint is sync, so FastAPI runs
        # it in a worker thread and those calls are served alongside it.
        verdict = review.reply(RestClient(record["url"]), record, req.message)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"run_id": run_id, "round": len(record["rounds"]), "status": record["status"],
            "verdict": record["rounds"][-1]["verdict"], "review": verdict.review,
            "text": verdict.pretty()}


@app.get("/health")
def health():
    return {"status": "ok"}
