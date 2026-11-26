"""Feedback store: every run, the engineer's replies and ratings, and what they teach the tool.

Files (under feedback/, gitignored; set BGP_FEEDBACK_DIR to move them):
  runs/<run_id>.json   one record per run: the question, every round (verdict, why it was
                       flagged, the engineer's reply), the engineer's rating, the evidence
  history.jsonl        one line per event (engineer_reply, rated), for metrics
  cases.jsonl          confirmed/corrected cases in the ML engine's training format

Run status:
  auto        confident; nobody was asked
  pending     flagged as uncertain (or re-investigated) and waiting for the engineer
  confirmed   the engineer said the latest diagnosis is right
  corrected   the engineer said it is wrong and gave the real fault class
  unresolved  the engineer said it is wrong, without the real fault class

What it feeds:
  - the agent, within a run: each reply goes back into the next investigation
  - the ML engine: `python3 -m analyzer.ml_engine train` adds cases.jsonl (confirmed
    and corrected runs) to its synthetic training set
  - metrics: `stats` shows how often runs are flagged, whether flagged runs really are
    wrong more often than unflagged ones, which warning signs are worth asking on,
    and how many replies it takes to get a confirmed answer

Usage:
    python3 -m analyzer.feedback pending
    python3 -m analyzer.feedback reply <run_id> "the neighbor is shut down on r2; check bgp_neighbor"
    python3 -m analyzer.feedback rate <run_id> --correct
    python3 -m analyzer.feedback rate <run_id> --wrong --actual neighbor_shutdown --comment "..."
    python3 -m analyzer.feedback stats
"""
import argparse
import json
import logging
import os
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

FEEDBACK_DIR = Path(os.getenv("BGP_FEEDBACK_DIR",
                              Path(__file__).resolve().parent.parent / "feedback"))
CASES_PATH = FEEDBACK_DIR / "cases.jsonl"
DEVICE_TOOLS = {"bgp_state", "bgp_neighbor", "interface", "tcp_port", "config",
                "route_table", "route_map", "route_to_peer", "prefix_list"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


def _runs_dir() -> Path:
    d = FEEDBACK_DIR / "runs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(run_id: str) -> Path:
    if not run_id or len(run_id) > 40 or any(c not in "0123456789abcdef-" for c in run_id):
        raise ValueError(f"bad run id {run_id!r}")
    return _runs_dir() / f"{run_id}.json"


def save(record: dict) -> None:
    record.setdefault("time", now())
    _path(record["run_id"]).write_text(json.dumps(record, indent=1, default=str), encoding="utf-8")


def load(run_id: str) -> dict:
    path = _path(run_id)
    if not path.exists():
        raise KeyError(f"no run {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def event(kind: str, run_id: str, **fields) -> None:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    with open(FEEDBACK_DIR / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": now(), "event": kind, "run_id": run_id, **fields}, default=str) + "\n")


def all_runs() -> list[dict]:
    runs = []
    for p in sorted(_runs_dir().glob("*.json")):
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("Skipping unreadable run record %s: %s", p, e)
    return runs


def pending() -> list[dict]:
    """Runs waiting for the engineer, newest first, in a short form for a list or dashboard."""
    out = []
    for r in reversed(all_runs()):
        if r.get("status") == "pending":
            last = r["rounds"][-1]
            out.append({"run_id": r["run_id"], "time": r.get("time"), "question": r["question"],
                        "host": r["host"], "peer": r["peer"], "rounds": len(r["rounds"]),
                        "fault_class": last["verdict"]["fault_class"],
                        "root_cause": last["verdict"]["root_cause"],
                        "reasons": last["review"]["reasons"]})
    return out


def confirmed_label(record: dict) -> str | None:
    """The fault class this run is known to be, or None if the engineer didn't settle it."""
    op = record.get("operator") or {}
    if record.get("status") == "confirmed":
        return record["rounds"][-1]["verdict"]["fault_class"]
    if record.get("status") == "corrected":
        return op.get("actual_fault_class")
    return None


def export_cases(path: Path | None = None) -> int:
    """Rewrite cases.jsonl in the ML engine's format from every confirmed/corrected run.

    Only labels the ML engine knows (ml_engine.FAULTS) are written: route classes and
    "other" aren't ML classes, and a model trained on extra labels would be rejected
    when it is loaded.
    """
    from analyzer.ml_engine import FAULTS
    path = path or CASES_PATH
    n = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in all_runs():
            label = confirmed_label(r)
            evidence = [e for e in r.get("evidence") or [] if e.get("tool") in DEVICE_TOOLS]
            if label in FAULTS and evidence:
                f.write(json.dumps({"run_id": r["run_id"], "peer": r.get("peer"),
                                    "label": label, "evidence": evidence}, default=str) + "\n")
                n += 1
    log.info("Exported %d labelled cases to %s", n, path)
    return n


def _rate(hits: int, total: int):
    return round(hits / total, 2) if total else None


def stats() -> dict:
    """Is the confidence check asking about the right runs, and does the engineer's reply help?

    - flagged_rate: share of runs sent to the engineer (too high = people stop reading)
    - accuracy_when_flagged vs accuracy_when_not_flagged: the check works if flagged runs
      are wrong much more often. Rate some unflagged runs too, or the second number is empty.
    - by_signal: for each warning sign, how often it fired and how often the first diagnosis
      was right anyway (a sign that fires on mostly-right runs is noise)
    - replies: how many runs needed engineer replies, and how many ended confirmed
    """
    runs = all_runs()
    status = Counter(r.get("status") for r in runs)
    first_right = defaultdict(Counter)    # bucket -> rated / right
    by_signal = defaultdict(Counter)
    replies = Counter()
    for r in runs:
        first = r["rounds"][0]
        flagged = first["review"]["needed"]
        n_replies = sum(1 for x in r["rounds"] if x.get("engineer_says"))
        for code in first["review"].get("codes", []):
            by_signal[code]["fired"] += 1
        if n_replies:
            replies["runs_with_replies"] += 1
            replies["total_replies"] += n_replies
            replies["confirmed_after_reply"] += r.get("status") == "confirmed"
        if r.get("status") in ("confirmed", "corrected", "unresolved"):
            # the first diagnosis was right only if it was confirmed with no reply needed
            right = r["status"] == "confirmed" and n_replies == 0
            bucket = "flagged" if flagged else "not_flagged"
            first_right[bucket]["rated"] += 1
            first_right[bucket]["right"] += right
            for code in first["review"].get("codes", []):
                by_signal[code]["rated"] += 1
                by_signal[code]["right"] += right

    return {
        "runs": len(runs),
        "status": dict(status),
        "flagged_rate": _rate(sum(r["rounds"][0]["review"]["needed"] for r in runs), len(runs)),
        "accuracy_when_flagged": _rate(first_right["flagged"]["right"], first_right["flagged"]["rated"]),
        "accuracy_when_not_flagged": _rate(first_right["not_flagged"]["right"],
                                           first_right["not_flagged"]["rated"]),
        "rated": {k: dict(v) for k, v in first_right.items()},
        "by_signal": {code: {**c, "right_anyway": _rate(c["right"], c["rated"])}
                      for code, c in sorted(by_signal.items())},
        "replies": {**replies,
                    "avg_replies": _rate(replies["total_replies"], replies["runs_with_replies"]),
                    "confirmed_after_reply_rate": _rate(replies["confirmed_after_reply"],
                                                        replies["runs_with_replies"])},
    }


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description="Engineer feedback on troubleshooter runs.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pending", help="runs waiting for an engineer")
    rp = sub.add_parser("reply", help="tell the agent what's wrong; it re-investigates")
    rp.add_argument("run_id")
    rp.add_argument("message")
    rp.add_argument("--url", help="tool API URL (default: the one the run used)")
    rate = sub.add_parser("rate", help="say whether a run's latest diagnosis is right")
    rate.add_argument("run_id")
    g = rate.add_mutually_exclusive_group(required=True)
    g.add_argument("--correct", action="store_true")
    g.add_argument("--wrong", action="store_true")
    rate.add_argument("--actual", help="the real fault class, when the diagnosis was wrong")
    rate.add_argument("--comment")
    sub.add_parser("stats", help="how well the confidence check and the replies work")
    sub.add_parser("export", help="rewrite cases.jsonl for ML training")
    args = ap.parse_args(argv)

    from analyzer import review   # imports the agent; only needed here
    if args.cmd == "pending":
        print(json.dumps(pending(), indent=1))
    elif args.cmd == "reply":
        from analyzer.rest_client import RestClient
        from analyzer.run import setup_run_log
        record = load(args.run_id)
        log_path = setup_run_log(Path(__file__).resolve().parent.parent / "logs")
        verdict = review.reply(RestClient(args.url or record["url"]), record, args.message)
        print(verdict.pretty())
        print(f"Run id:        {record['run_id']} (round {len(record['rounds'])})")
        print(f"Log file:      {log_path}")
        print(f"If this is right: python3 -m analyzer.feedback rate {record['run_id']} --correct")
    elif args.cmd == "rate":
        print(json.dumps(review.rate(load(args.run_id), args.correct, args.actual, args.comment), indent=1))
    elif args.cmd == "stats":
        print(json.dumps(stats(), indent=1))
    else:
        n = export_cases()
        print(f"Wrote {n} cases to {CASES_PATH}")


if __name__ == "__main__":
    main()
