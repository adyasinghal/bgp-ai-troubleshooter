import argparse
import sys
from analyzer.rest_client import RestClient
from analyzer.rules_engine import diagnose

# Ensure UTF-8 stdout encoding on Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Deterministic + LLM-Assisted Network Troubleshooter")
    ap.add_argument("question", help="Troubleshooting query")
    ap.add_argument("--host", required=True, help="Target device/host")
    ap.add_argument("--peer", required=True, help="Target BGP peer IP")
    ap.add_argument("--url", default="http://localhost:8000", help="Tool cohort API base URL")
    ap.add_argument("--llm", action="store_true", help="Enable LLM-assisted diagnosis and explanation")
    args = ap.parse_args()
    print(diagnose(RestClient(args.url), args.question, args.host, args.peer, enable_llm=args.llm).pretty())


if __name__ == "__main__":
    main()