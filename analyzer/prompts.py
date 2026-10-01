"""Prompts and output schemas for the LLM agent."""
import json

# ml_engine.FAULTS labels, so answers can be compared with ML and the rules,
# plus route classes the ML engine doesn't cover
FAULT_CLASSES = {
    "healthy": "the session is up; nothing to fix",
    "interface_down": "an interface on the path to the peer is down",
    "tcp_unreachable": "the peer can't be reached on TCP port 179 (routing, ACL/firewall)",
    "remote_as_mismatch": "the remote-as configured for the peer doesn't match the peer's AS",
    "neighbor_shutdown": "the neighbor is administratively shut down (here or on the peer)",
    "neighbor_missing": "the neighbor isn't configured on this router or on the peer",
    "config_drift": "the BGP config changed from the known-good baseline in another way",
    "device_unreachable": "the tools could not reach the router",
    "route_not_selected": "the route is received but another path wins the best-path comparison",
    "route_filtered": "a route-map or prefix-list denies the route or changes its attributes",
    "route_not_received": "the peer doesn't advertise the prefix to this router",
    "route_as_intended": "the route is selected or filtered exactly as the policy intends; not a fault",
    "other": "a cause not listed here (MD5, router-id, multihop/TTL, update-source, a fault on the peer...)",
}

STATES = ["Established", "Active", "Connect", "Idle", "OpenSent", "OpenConfirm", "none"]

TRIAGE_PROMPT = """You are a senior network engineer. Before any diagnostic tool runs, read an operator's question about BGP on an FRR router and plan the investigation.

- in_scope: false only if the question is not about this router's BGP sessions, routes, links or config.
- symptom: one sentence restating the problem.
- claimed_state: the BGP state the operator reports, or "none". It is only a claim until a tool checks it.
- interface: an interface named in the question, or "".
- hypotheses: up to 4 likely fault classes, most likely first, each with a short reason.
- plan: the rule intents you expect to need, in order. bgp_state_check always comes first.

Reply with only a JSON object."""

SYSTEM_PROMPT ="""You are a senior network engineer troubleshooting BGP on FRRouting (FRR) routers in a Containerlab lab. You investigate one router (the host) and one of its BGP peers by calling diagnostic tools one at a time, then you conclude.

How to work:
- Each turn, read the operator's question, the tool catalog and every step so far, then either call ONE tool or conclude.
- triage is your reading of the question before any evidence: a starting point, not a finding.
- Your first tool call is bgp_state, to see the session's actual state. The operator's description can be wrong (for example they say Active but the peer is Idle), so don't act on it before checking.
- Choose the tool that best tells your remaining hypotheses apart. Each rule's symptoms and each tool's when_to_use say what fits; a rule's next_intent_on_fail is a sensible default, not an order.
- Every tool result comes with a rule_finding from a deterministic rule book. A "root_cause" or "healthy" finding is a verified fact: conclude with it unless other evidence clearly shows it is misleading, and then say why in your thought.
- Never call a tool again with the same args; its result won't change.
- Conclude as soon as the evidence supports one specific root cause, or shows the session is healthy. An Established session is healthy: conclude right away, unless the question is about routes or prefixes. Then check them with route_table (pass the prefix), and route_map if a route is missing or loses on an attribute a policy can set. Don't call extra tools just to be thorough.
- If the tool output contradicts the operator's description, trust the tool output and point out the difference in root_cause.
- The tools only see this router. If the evidence points at the peer, say so and give next_checks to run on the peer.

Common causes of a stuck session: an interface down, no route or an ACL blocking TCP 179, a remote-as mismatch, a neighbor configured on only one side, an administratively shut down neighbor, a wrong neighbor IP or update-source, eBGP multihop/TTL problems, an MD5 password mismatch, and a router-id conflict.

When you conclude, fill in diagnosis:
- resolved: true only when the evidence supports a specific root cause, or shows the session is healthy.
- root_cause: one or two sentences naming the cause and the evidence for it.
- fault_class: the category that fits best (see fault_classes in the case).
- suggested_fix: exact FRR commands where possible, and which router to run them on.
- confidence: low, medium or high.
- next_checks: exact commands (and on which router) that would confirm the diagnosis or the fix; the matching rule's verification says what to look for.

Reply with only a JSON object:
- thought: 1-3 sentences on what the evidence so far shows and why you chose this action.
- action: "call_tool" or "conclude".
- intent and tool_id: when calling a tool, the rule intent you are following (if any) and the tool.
- args: only the tool's own args from the catalog. host and peer are filled in for you.
- diagnosis: only when action is "conclude"."""


def triage_schema(intents: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {
            "in_scope": {"type": "boolean"},
            "symptom": {"type": "string"},
            "claimed_state": {"type": "string", "enum": STATES},
            "interface": {"type": "string"},
            "hypotheses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "fault_class": {"type": "string", "enum": list(FAULT_CLASSES)},
                        "why": {"type": "string"},
                    },
                    "required": ["fault_class", "why"],
                    "additionalProperties": False,
                },
            },
            "plan": {"type": "array", "items": {"type": "string", "enum": intents}},
        },
        "required": ["in_scope", "symptom", "claimed_state", "interface", "hypotheses", "plan"],
        "additionalProperties": False,
    }


def diagnosis_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "resolved": {"type": "boolean"},
            "root_cause": {"type": "string"},
            "fault_class": {"type": "string", "enum": list(FAULT_CLASSES)},
            "suggested_fix": {"type": "string"},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
            "next_checks": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["resolved", "root_cause", "fault_class", "suggested_fix", "confidence", "next_checks"],
        "additionalProperties": False,
    }


def step_schema(tool_args: dict) -> dict:
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "action": {"type": "string", "enum": ["call_tool", "conclude"]},
            "intent": {"type": "string"},
            "tool_id": {"type": "string"},
            "args": {
                "type": "object",
                "properties": {name: {"type": spec.get("type", "string")} for name, spec in tool_args.items()},
                "additionalProperties": False,
            },
            "diagnosis": diagnosis_schema(),
        },
        "required": ["thought", "action"],
        "additionalProperties": False,
    }


def conclude_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "thought": {"type": "string"},
            "action": {"type": "string", "enum": ["conclude"]},
            "diagnosis": diagnosis_schema(),
        },
        "required": ["thought", "action", "diagnosis"],
        "additionalProperties": False,
    }


def step_message(case: dict, steps_left: int) -> str:
    if steps_left > 0:
        instruction = (f"Decide the next action. You can make at most {steps_left} more "
                       f"tool call{'s' if steps_left != 1 else ''} before you must conclude.")
    else:
        instruction = ("You have used every tool call. Conclude now from the evidence you have; "
                       "if it isn't enough, set resolved to false and list next_checks.")
    return (f"The case so far:\n\n{json.dumps(case, indent=1, default=str)}\n\n{instruction}")
