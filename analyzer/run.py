import argparse
from analyzer.rest_client import RestClient
from analyzer.rules_engine import diagnose


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--host", required=True)
    ap.add_argument("--peer", required=True)
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--no-ml", action="store_true", help="skip the ML engine")
    ap.add_argument("--no-llm", action="store_true", help="skip LLM escalation")
    args = ap.parse_args()
    verdict = diagnose(RestClient(args.url), args.question, args.host, args.peer,
                       use_ml=not args.no_ml, use_llm=not args.no_llm)
    print(verdict.pretty())


if __name__ == "__main__":
    main()
