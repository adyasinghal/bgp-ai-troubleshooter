import argparse
import logging
from datetime import datetime
from pathlib import Path

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--host", required=True)
    ap.add_argument("--peer", required=True)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--no-ml", action="store_true", help="skip the ML engine")
    ap.add_argument("--no-llm", action="store_true", help="skip LLM escalation")
    ap.add_argument("--log-dir", type=Path, default=LOG_DIR,
                    help=f"where to write this run's log file (default: {LOG_DIR})")
    args = ap.parse_args()

    log_path = setup_run_log(args.log_dir)
    log.info("Run started: question=%r host=%s peer=%s url=%s use_ml=%s use_llm=%s",
             args.question, args.host, args.peer, args.url, not args.no_ml, not args.no_llm)
    try:
        verdict = diagnose(RestClient(args.url), args.question, args.host, args.peer,
                           use_ml=not args.no_ml, use_llm=not args.no_llm)
    except Exception:
        log.exception("Run failed")
        print(f"Log file:      {log_path}")
        raise
    log.info("Verdict:\n%s", verdict.pretty())
    log.info("Run finished")
    print(verdict.pretty())
    print(f"Log file:      {log_path}")


if __name__ == "__main__":
    main()
