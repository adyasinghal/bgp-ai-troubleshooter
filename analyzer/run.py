import argparse
from analyzer.rest_client import RestClient
from analyzer.rules_engine import diagnose


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--host", required=True)
    ap.add_argument("--peer", required=True)
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()
    print(diagnose(RestClient(args.url), args.question, args.host, args.peer).pretty())


if __name__ == "__main__":
    main()