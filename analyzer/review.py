"""Engineer review: when a verdict looks uncertain, ask the engineer, and let them reply.

The loop, for one run:

    verdict --> confidence.assess() --> confident: done (saved, status "auto")
                      |
                      v  uncertain
              show the engineer why  -->  "Is this right?"
                      |                      y -> status "confirmed" (becomes ML training data)
                      |                      Enter -> status "pending" (answer later: CLI or API)
                      v  n
              engineer writes what is wrong / what to check / how to fix it
                      |
                      v
              agent picks up where it left off: the earlier tool results carry over as
              steps already done, plus every earlier conclusion and the reply
              (analyzer/agent.py: carried_steps, engineer_feedback)
                      |
                      +--> new verdict, asked again (up to max_rounds replies)
                      |
                      v  still wrong after the last round
              engineer picks the real fault class -> status "corrected", or skips -> "unresolved"

The same steps run from the CLI (analyzer.run, analyzer.feedback reply) and from the
API (POST /feedback/{run_id}); analyzer/feedback.py stores every round.
"""
import json
import logging

from analyzer import confidence, feedback
from analyzer.agent import DEFAULT_MAX_STEPS, investigate
from analyzer.prompts import FAULT_CLASSES

log = logging.getLogger(__name__)

MAX_MESSAGE = 2000     # characters of engineer text passed to the LLM
DEFAULT_MAX_ROUNDS = 3
MAX_CARRIED = 8        # most tool results carried into the next round (prompt size, esp. on qwen)


def summarize(verdict) -> dict:
    return {
        "resolved": verdict.resolved, "root_cause": verdict.root_cause,
        "suggested_fix": verdict.suggested_fix, "fault_class": verdict.fault_class,
        "source": verdict.source, "confidence": verdict.confidence,
        "rules_agree": verdict.rules_agree,
        "ml": ({"label": verdict.ml_prediction["label"], "confidence": verdict.ml_prediction["confidence"]}
               if verdict.ml_prediction else None),
        "checked": verdict.checked,
        "steps": [f"{s.get('tool') or '-'}: {s.get('summary') or 'rejected (' + str(s.get('rejected')) + ')'}"
                  + (f" (from round {s['carried_over_from_round']})" if s.get("carried_over_from_round") else "")
                  for s in verdict.steps],
        "notes": verdict.notes,
    }


def start(question: str, host: str, peer: str | None, url: str, mode: str,
          verdict, ask_on: str = "low", max_steps: int = DEFAULT_MAX_STEPS,
          use_ml: bool = True, use_llm: bool = True) -> dict:
    """Assess a fresh verdict and save the run. Returns the run record."""
    review = confidence.assess(verdict, ask_on)
    verdict.review = review.to_dict()
    record = {
        "run_id": feedback.new_run_id(),
        "question": question, "host": host, "peer": peer, "url": url, "mode": mode,
        "ask_on": ask_on, "max_steps": max_steps, "use_ml": use_ml, "use_llm": use_llm,
        "status": "pending" if review.needed else "auto",
        "rounds": [],
        "operator": None,
    }
    _add_round(record, verdict, review)
    feedback.save(record)
    log.info("Run %s saved: review needed=%s %s", record["run_id"], review.needed, review.codes)
    return record


def _add_round(record: dict, verdict, review) -> None:
    number = len(record["rounds"]) + 1
    if verdict.work:
        work = verdict.work
    else:   # rules mode: no agent steps, so carry the rule chain's device evidence
        work = [{"tool": e["tool"], "args": {}, "result": e} for e in verdict.evidence
                if e.get("tool") in feedback.DEVICE_TOOLS]
    record["rounds"].append({
        "round": number,
        "time": feedback.now(),
        "verdict": summarize(verdict),
        "review": review.to_dict(),
        "engineer_says": None,
        "work": [{**w, "round": w.get("round") or number} for w in work],
    })
    record["evidence"] = verdict.evidence     # the evidence behind the latest verdict


def _carried_steps(record: dict) -> list[dict]:
    """The latest round's tool results (which include what it carried), newest kept per check."""
    latest = {}
    for w in record["rounds"][-1].get("work", []):
        latest.pop((w["tool"], json.dumps(w.get("args") or {}, sort_keys=True)), None)
        latest[(w["tool"], json.dumps(w.get("args") or {}, sort_keys=True))] = w
    return list(latest.values())[-MAX_CARRIED:]


def _feedback_for_agent(record: dict) -> list[dict]:
    """Every round the engineer replied to, as the agent's engineer_feedback."""
    out = []
    for r in record["rounds"]:
        if r.get("engineer_says"):
            v = r["verdict"]
            out.append({
                "round": r["round"],
                "your_conclusion_then": {k: v[k] for k in ("fault_class", "root_cause", "suggested_fix",
                                                           "confidence")},
                "why_it_was_flagged": r["review"]["reasons"],
                "checks_you_ran": v["steps"] or v["checked"],
                "engineer_says": r["engineer_says"],
            })
    return out


def reply(client, record: dict, message: str, max_rounds: int = DEFAULT_MAX_ROUNDS):
    """The engineer's reply to the latest round: re-investigate with it.

    Returns the new verdict (its review filled in). Agent mode only: the rule chain
    can't read free text, so a rules-mode run is re-investigated in agent mode.
    """
    message = (message or "").strip()
    if not record.get("use_llm", True):
        raise ValueError("this run was made with --no-llm, so a written reply can't be used; "
                         "rate it with the real fault class instead")
    if not message:
        raise ValueError("the reply is empty")
    if len(record["rounds"]) > max_rounds:
        raise ValueError(f"this run already had {max_rounds} replies; "
                         "rate it with the real fault class instead")
    record["rounds"][-1]["engineer_says"] = message[:MAX_MESSAGE]
    feedback.event("engineer_reply", record["run_id"], round=record["rounds"][-1]["round"],
                   message=message[:MAX_MESSAGE])
    verdict = investigate(client, record["question"], record["host"], record["peer"],
                          max_steps=record.get("max_steps", DEFAULT_MAX_STEPS),
                          use_ml=record.get("use_ml", True),
                          engineer_feedback=_feedback_for_agent(record),
                          carried_steps=_carried_steps(record))
    review = confidence.assess(verdict, record.get("ask_on", "low"))
    verdict.review = review.to_dict()
    if any("fell back to the rule chain" in n for n in verdict.notes):
        verdict.notes.append("The LLM was unavailable, so the engineer's reply could not be used")
    _add_round(record, verdict, review)
    record["status"] = "pending"     # the engineer still has to say whether this one is right
    feedback.save(record)
    return verdict


def rate(record: dict, correct: bool, actual_fault_class: str | None = None,
         comment: str | None = None) -> dict:
    """The engineer's verdict on the latest round."""
    if actual_fault_class and actual_fault_class not in FAULT_CLASSES:
        raise ValueError(f"unknown fault class {actual_fault_class!r}; one of: {', '.join(FAULT_CLASSES)}")
    if correct and actual_fault_class:
        raise ValueError("give actual_fault_class only when the diagnosis was wrong")
    record["operator"] = {"correct": bool(correct), "actual_fault_class": actual_fault_class,
                          "comment": comment, "round": len(record["rounds"]), "time": feedback.now()}
    record["status"] = ("confirmed" if correct else "corrected" if actual_fault_class else "unresolved")
    feedback.save(record)
    last = record["rounds"][-1]
    feedback.event("rated", record["run_id"], status=record["status"],
                   predicted=last["verdict"]["fault_class"], actual=actual_fault_class,
                   flagged=last["review"]["needed"], codes=last["review"]["codes"],
                   rounds=len(record["rounds"]), comment=comment)
    feedback.export_cases()
    return record["operator"]


# --- the CLI conversation ---

def interactive(client, record: dict, verdict, ask=input, say=print,
                max_rounds: int = DEFAULT_MAX_ROUNDS):
    """Ask the engineer about an uncertain verdict at the terminal. Returns the final verdict."""
    if not verdict.review or not verdict.review.get("needed"):
        return verdict
    say("\nI'm not confident about this diagnosis:")
    say("\n".join(f"  - {r}" for r in verdict.review["reasons"]))
    while True:
        answer = ask("Is it right? [y] yes  [n] no, I'll tell you what's wrong  [Enter] review later: ").strip().lower()
        if answer in ("y", "yes"):
            rate(record, True)
            say(f"Thanks, recorded as confirmed (run {record['run_id']}).")
            return verdict
        if answer not in ("n", "no"):
            say(f"Left for review. Answer later with: python3 -m analyzer.feedback reply {record['run_id']} \"...\"")
            return verdict

        if len(record["rounds"]) > max_rounds or not record.get("use_llm", True):
            return _final_correction(record, verdict, ask, say, max_rounds)
        say("Tell me what's wrong, what to check, or how to fix it. For example:\n"
            "  \"the neighbor is shut down on r2, not here; check bgp_neighbor\"")
        message = ask("> ").strip()
        if not message:
            say(f"No reply given; left for review (run {record['run_id']}).")
            return verdict
        say("Re-investigating with your note...\n")
        verdict = reply(client, record, message, max_rounds)
        say(verdict.pretty())
        say("")


def _final_correction(record: dict, verdict, ask, say, max_rounds: int):
    if record.get("use_llm", True):
        say(f"That was the last of {max_rounds} replies. What was the real fault class?")
    else:
        say("This run doesn't use the LLM (--no-llm), so it can't act on a written reply. "
            "What was the real fault class?")
    say("  " + ", ".join(FAULT_CLASSES))
    actual = ask("Fault class [Enter to skip]: ").strip()
    while actual and actual not in FAULT_CLASSES:
        actual = ask("Not a known fault class; try again [Enter to skip]: ").strip()
    comment = ask("Anything to add? [Enter to skip]: ").strip() or None
    rate(record, False, actual or None, comment)
    say(f"Recorded as {record['status']} (run {record['run_id']}).")
    return verdict
