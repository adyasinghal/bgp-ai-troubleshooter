"""ML engine: classify the most likely BGP fault from the tools' evidence.

The rule chain only fires on single, clear-cut signals (interface down, port
179 closed, config drifted from a baseline) and stops at the first one. The
ML engine looks at all the collected evidence together and names the most
likely fault class with a confidence score. The rules engine uses it to:
  - resolve cases the rule chain could not (when confidence is high enough)
  - give a second opinion on cases the rules did resolve
  - give the LLM a starting hypothesis when a case is escalated

The model is a RandomForest trained on synthetic cases built from known BGP
failure patterns (see _sample). As real cases are collected, label them and
retrain:
    python3 -m analyzer.ml_engine train --cases cases.jsonl
where each line is {"evidence": [...], "peer": "172.20.20.3", "label": "remote_as_mismatch"}.
"""
import argparse
import json
import random
from dataclasses import dataclass, asdict
from pathlib import Path

import joblib
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import classification_report
from sklearn.model_selection import train_test_split

MODEL_PATH = Path(__file__).parent / "models" / "bgp_fault_model.joblib"

PEER_STATES = ["Established", "Active", "Connect", "Idle", "OpenSent", "OpenConfirm"]

FEATURES = [
    # bgp_state: one-hot of the queried peer's state
    *[f"peer_{s.lower()}" for s in PEER_STATES],
    "peer_unknown",          # peer missing from `show bgp summary`
    "peer_not_checked",
    "peer_admin_shutdown",   # "Idle (Admin)" on the peer's summary line
    # interface
    "iface_checked", "ifaces_down",
    # tcp_port
    "tcp_checked", "tcp_unreachable",
    # config: running config (works without a baseline)
    "config_checked", "cfg_router_bgp", "cfg_peer_configured", "cfg_peer_shutdown",
    # config: diff against the baseline (only when a baseline exists)
    "config_has_baseline", "config_drifted",
    "diff_remote_as", "diff_neighbor", "diff_shutdown", "diff_router_bgp",
    # tools that failed to run (SSH errors etc.)
    "tool_errors",
]

# label -> (root cause, suggested fix); {peer} is filled in at predict time
FAULTS = {
    "healthy": (
        "No fault pattern found; the session looks healthy",
        "No action needed.",
    ),
    "interface_down": (
        "An interface on the path to peer {peer} is down",
        "Bring it up: conf t / interface <name> / no shutdown",
    ),
    "tcp_unreachable": (
        "Peer {peer} is not reachable on TCP port 179",
        "Check routing to the peer and any ACL/firewall blocking port 179.",
    ),
    "remote_as_mismatch": (
        "The remote-as configured for {peer} likely does not match the peer's local AS",
        "Compare `show bgp neighbors {peer}` with the peer's `router bgp <asn>`, "
        "then fix with: router bgp <local-asn> / neighbor {peer} remote-as <peer-asn>",
    ),
    "neighbor_shutdown": (
        "Neighbor {peer} is administratively shut down",
        "router bgp <local-asn> / no neighbor {peer} shutdown",
    ),
    "neighbor_missing": (
        "Neighbor {peer} is not configured on this router",
        "router bgp <local-asn> / neighbor {peer} remote-as <peer-asn>",
    ),
    "config_drift": (
        "BGP configuration has drifted from the baseline",
        "Review the config diff and restore the correct `router bgp` / neighbor settings.",
    ),
    "device_unreachable": (
        "The diagnostic tools could not reach the router",
        "Check the container is running, its management IP, and the SSH credentials in device_client.py.",
    ),
    "unknown": (
        "Evidence does not match a known fault pattern",
        "Escalate for deeper analysis.",
    ),
}


@dataclass
class MLPrediction:
    label: str
    confidence: float
    probabilities: dict[str, float]

    def describe(self, peer: str | None) -> tuple[str, str]:
        cause, fix = FAULTS[self.label]
        return cause.format(peer=peer or "<peer>"), fix.format(peer=peer or "<peer>")

    def to_dict(self) -> dict:
        return asdict(self)


# --- feature extraction ---

def _changed_lines(diff: list[str]) -> str:
    return "\n".join(l for l in diff
                     if l[:1] in "+-" and not l.startswith(("+++", "---")))


def extract_features(evidence: list[dict], peer: str | None = None) -> dict:
    """Turn the rules engine's evidence list into a flat feature dict."""
    f = dict.fromkeys(FEATURES, 0)
    by_tool = {}
    for e in evidence:
        if e.get("success") is False:
            f["tool_errors"] += 1
        else:
            by_tool[e["tool"]] = e

    bgp = by_tool.get("bgp_state")
    if bgp:
        state = (bgp.get("parsed") or {}).get("queried_peer_state", "unknown")
        f[f"peer_{state.lower()}" if state in PEER_STATES else "peer_unknown"] = 1
        f["peer_admin_shutdown"] = int(any(
            line.split()[:1] == [peer] and "(Admin)" in line
            for line in (bgp.get("raw_output") or "").splitlines()
        ))
    else:
        f["peer_not_checked"] = 1

    iface = by_tool.get("interface")
    if iface:
        ifaces = (iface.get("parsed") or {}).get("interfaces", {})
        f["iface_checked"] = 1
        f["ifaces_down"] = sum(1 for i in ifaces.values()
                               if "down" in (i.get("link_state"), i.get("admin_state")))

    tcp = by_tool.get("tcp_port")
    if tcp:
        f["tcp_checked"] = 1
        f["tcp_unreachable"] = int((tcp.get("parsed") or {}).get("reachable") is False)

    cfg = by_tool.get("config")
    if cfg:
        parsed = cfg.get("parsed") or {}
        running = cfg.get("raw_output") or ""
        f["config_checked"] = 1
        f["cfg_router_bgp"] = int("router bgp" in running)
        if peer:
            f["cfg_peer_configured"] = int(f"neighbor {peer} " in running)
            f["cfg_peer_shutdown"] = int(f"neighbor {peer} shutdown" in running)
        if parsed.get("has_baseline"):
            changed = _changed_lines(parsed.get("diff", []))
            f["config_has_baseline"] = 1
            f["config_drifted"] = int(bool(changed))
            f["diff_remote_as"] = int("remote-as" in changed)
            f["diff_neighbor"] = int("neighbor" in changed)
            f["diff_shutdown"] = int("shutdown" in changed)
            f["diff_router_bgp"] = int("router bgp" in changed)
    return f


def _vector(f: dict) -> list[int]:
    return [f[k] for k in FEATURES]


# --- synthetic training data ---

NOISY = ["peer_admin_shutdown", "cfg_peer_shutdown", "diff_neighbor",
         "diff_router_bgp", "tcp_unreachable", "cfg_router_bgp"]


def _sample(label: str, rng: random.Random) -> dict:
    """One synthetic feature dict that looks like a real case of `label`."""
    f = dict.fromkeys(FEATURES, 0)
    maybe = lambda p=0.5: rng.random() < p
    stuck = lambda: rng.choice(["Active", "Connect", "Idle"])

    def peer(state):
        f[f"peer_{state.lower()}"] = 1

    def iface(down=0):
        f["iface_checked"], f["ifaces_down"] = 1, down

    def tcp(unreachable=0):
        f["tcp_checked"], f["tcp_unreachable"] = 1, unreachable

    def config(baseline, diff=(), running=("cfg_router_bgp", "cfg_peer_configured")):
        f["config_checked"] = 1
        for k in running:
            f[k] = 1
        if baseline:
            f["config_has_baseline"] = 1
            if diff:
                f["config_drifted"] = 1
                for d in diff:
                    f[d] = 1

    # Cases the rules catch stop early, so later tools are often unchecked.
    if label == "healthy":
        if maybe(0.7):   # rules stop right after an Established bgp_state
            peer("Established")
        else:
            f["peer_not_checked"] = 1
            iface(); tcp(); config(maybe(0.6))
    elif label == "interface_down":
        peer(stuck()); iface(down=rng.randint(1, 2))
        if maybe(0.3):
            tcp(unreachable=int(maybe(0.7)))
    elif label == "tcp_unreachable":
        peer(rng.choice(["Active", "Connect"])); iface(); tcp(unreachable=1)
        if maybe(0.3):
            config(maybe(0.5))
    elif label == "remote_as_mismatch":
        iface(); tcp()
        if maybe(0.7):   # baseline shows the remote-as line changed
            peer(rng.choice(["Active", "Idle", "OpenSent", "Connect"]))
            config(True, diff=("diff_remote_as", "diff_neighbor"))
        else:            # no baseline: OPEN exchange keeps failing
            peer(rng.choice(["OpenSent", "OpenConfirm", "Idle"]))
            config(False)
    elif label == "neighbor_shutdown":
        peer("Idle"); f["peer_admin_shutdown"] = int(maybe(0.9))
        iface(); tcp()
        config(maybe(0.5), diff=("diff_shutdown", "diff_neighbor"),
               running=("cfg_router_bgp", "cfg_peer_configured", "cfg_peer_shutdown"))
    elif label == "neighbor_missing":
        f["peer_unknown"] = 1; iface(); tcp()
        config(maybe(0.5), diff=("diff_neighbor", "diff_remote_as"), running=("cfg_router_bgp",))
    elif label == "config_drift":
        peer(stuck()); iface(); tcp()
        config(True, diff=rng.choice([("diff_router_bgp",), ("diff_neighbor",),
                                      ("diff_router_bgp", "diff_neighbor")]))
    elif label == "device_unreachable":
        f["peer_not_checked"] = 1
        f["tool_errors"] = rng.randint(2, 4)
    elif label == "unknown":
        # OpenSent/OpenConfirm also fit router-id conflicts, MD5 or capability
        # mismatches, so they overlap with remote_as_mismatch on purpose.
        peer(stuck() if maybe(0.6) else rng.choice(["OpenSent", "OpenConfirm"]))
        iface(); tcp(); config(maybe(0.5))

    if label != "device_unreachable" and maybe(0.05):
        f["tool_errors"] = 1
    for k in NOISY:
        if maybe(0.02):
            f[k] = 1 - f[k]
    return f


def _synthetic_dataset(per_label: int = 400, seed: int = 42):
    rng = random.Random(seed)
    X, y = [], []
    for label in FAULTS:
        for _ in range(per_label):
            X.append(_vector(_sample(label, rng)))
            y.append(label)
    return X, y


def _load_cases(path: Path):
    X, y = [], []
    for line in path.read_text().splitlines():
        if line.strip():
            case = json.loads(line)
            X.append(_vector(extract_features(case["evidence"], case.get("peer"))))
            y.append(case["label"])
    return X, y


# --- train / predict ---

def train(cases_path: Path | None = None, verbose: bool = False) -> RandomForestClassifier:
    X, y = _synthetic_dataset()
    if cases_path:
        cx, cy = _load_cases(cases_path)
        X, y = X + cx, y + cy

    if verbose:
        X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)
        held_out = RandomForestClassifier(n_estimators=200, random_state=42).fit(X_tr, y_tr)
        print(classification_report(y_te, held_out.predict(X_te), zero_division=0))

    model = RandomForestClassifier(n_estimators=200, random_state=42).fit(X, y)
    MODEL_PATH.parent.mkdir(exist_ok=True)
    joblib.dump(model, MODEL_PATH)
    return model


_model = None


def _load_model() -> RandomForestClassifier:
    global _model
    if _model is None:
        try:
            _model = joblib.load(MODEL_PATH)
        except Exception:   # missing, or saved by an incompatible sklearn version
            _model = train()
    return _model


def predict(evidence: list[dict], peer: str | None = None) -> MLPrediction:
    model = _load_model()
    probs = model.predict_proba([_vector(extract_features(evidence, peer))])[0]
    ranked = sorted(zip(model.classes_, probs), key=lambda p: p[1], reverse=True)
    label, confidence = ranked[0]
    return MLPrediction(
        label=str(label),
        confidence=round(float(confidence), 3),
        probabilities={str(l): round(float(p), 3) for l, p in ranked if p > 0},
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Train the BGP fault classifier.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    tr = sub.add_parser("train")
    tr.add_argument("--cases", type=Path, help="JSONL of real labelled cases to add")
    args = ap.parse_args()
    train(args.cases, verbose=True)
    print(f"Saved model to {MODEL_PATH}")
