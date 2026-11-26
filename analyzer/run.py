import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from analyzer import review
from analyzer.agent import DEFAULT_MAX_STEPS, investigate
from analyzer.confidence import ASK_LEVELS
from analyzer.rest_client import RestClient
from analyzer.rules_engine import diagnose

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

log = logging.getLogger("analyzer.run")


def setup_run_log(log_dir: Path) -> Path:
    """Send every analyzer.* log record for this run to its own timestamped file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"run_{datetime.now():%Y%m%d-%H%M%S}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)-24s %(message)s"))
    logger = logging.getLogger("analyzer")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False   # file only; the console shows just the verdict
    return path


def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--host", required=True)
    ap.add_argument("--peer", required=True)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--mode", choices=["agent", "rules"], default="agent",
                    help="agent: the LLM picks each tool and concludes (default); "
                         "rules: fixed rule chain -> ML -> LLM only at the end")
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                    help=f"agent mode: most tool calls before it must conclude (default {DEFAULT_MAX_STEPS})")
    ap.add_argument("--trust-rules", action="store_true",
                    help="agent mode: stop as soon as a rule finds a root cause or a healthy session")
    ap.add_argument("--no-ml", action="store_true", help="skip the ML engine")
    ap.add_argument("--no-llm", action="store_true", help="skip the LLM entirely (implies --mode rules)")
    ap.add_argument("--log-dir", type=Path, default=LOG_DIR,
                    help=f"where to write this run's log file (default: {LOG_DIR})")
    ap.add_argument("--ask-on", choices=ASK_LEVELS, default="low",
                    help="when to ask the engineer to check the diagnosis: low (default) = when any "
                         "warning sign fires; medium = also when the LLM rates itself medium; "
                         "always; never")
    ap.add_argument("--no-ask", action="store_true",
                    help="flag uncertain runs for review but don't prompt (for scripts and test runs)")
    ap.add_argument("--max-rounds", type=int, default=review.DEFAULT_MAX_ROUNDS,
                    help=f"most engineer replies per run (default {review.DEFAULT_MAX_ROUNDS})")
    args = ap.parse_args(argv)
    if args.no_llm:
        args.mode = "rules"

    log_path = setup_run_log(args.log_dir)
    log.info("Run started: question=%r host=%s peer=%s url=%s mode=%s use_ml=%s use_llm=%s",
             args.question, args.host, args.peer, args.url, args.mode, not args.no_ml, not args.no_llm)
    try:
        client = RestClient(args.url)
        if args.mode == "agent":
            verdict = investigate(client, args.question, args.host, args.peer,
                                  max_steps=args.max_steps, use_ml=not args.no_ml,
                                  trust_rules=args.trust_rules)
        else:
            verdict = diagnose(client, args.question, args.host, args.peer,
                               use_ml=not args.no_ml, use_llm=not args.no_llm)
    except Exception:
        log.exception("Run failed")
        print(f"Log file:      {log_path}")
        raise
    record = review.start(args.question, args.host, args.peer, args.url, args.mode, verdict,
                          ask_on=args.ask_on, max_steps=args.max_steps, use_ml=not args.no_ml,
                          use_llm=not args.no_llm)
    log.info("Verdict:\n%s", verdict.pretty())
    print(verdict.pretty())
    print(f"Run id:        {record['run_id']}")
    print(f"Log file:      {log_path}")
    if verdict.review["needed"]:
        if args.no_ask or not sys.stdin.isatty():
            print(f"Waiting for review: python3 -m analyzer.feedback reply {record['run_id']} \"...\"")
        else:
            verdict = review.interactive(client, record, verdict, max_rounds=args.max_rounds)
    log.info("Run finished: status=%s rounds=%d", record["status"], len(record["rounds"]))


if __name__ == "__main__":
    main()
