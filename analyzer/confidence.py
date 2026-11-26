"""Confidence check: decide whether a verdict needs an engineer to look at it.

The LLM's own confidence (low/medium/high) is the weakest signal: models rate
themselves badly. The strongest is disagreement between the three engines: when
the rule book, the ML classifier and the LLM point at different causes, at least
one of them is wrong. A verdict is sent for review when any signal fires:

  - unresolved          no root cause was found
  - self-rated          the LLM rated its own confidence at or below the ask level
  - rules disagree      the agent's fault class doesn't match the rule book's finding
  - unsupported         the LLM concluded, but no rule finding backs it and the ML classifier
                        doesn't confidently agree, so nothing besides the LLM supports the answer
  - ML disagrees        the ML classifier confidently names a different fault class
  - weak ML decision    the ML classifier decided the case, but only just
  - tentative rule      no rule was conclusive; the chain fell back to its best guess
  - fault class other   the cause is outside the known fault classes

ask_on sets how cautious it is: "low" (default) reviews LLM verdicts rated low,
"medium" also reviews medium, "always" reviews every run, "never" turns it off.
"""
from dataclasses import dataclass, field

from analyzer.agent import AGREES_WITH, ROUTE_CLASSES

ASK_LEVELS = ("never", "low", "medium", "always")
ML_DISAGREE_MIN = 0.6     # ML must be at least this sure before its disagreement counts
ML_DECIDE_SAFE = 0.85     # an ML-decided verdict below this is a weak decision
ML_CLASSES_EXCLUDED = ROUTE_CLASSES | {"other", "unknown"}


@dataclass
class Review:
    needed: bool
    reasons: list[str] = field(default_factory=list)   # for people
    codes: list[str] = field(default_factory=list)     # one per reason, for stats

    def to_dict(self) -> dict:
        return {"needed": self.needed, "reasons": self.reasons, "codes": self.codes}


def _compatible(a: str, b: str) -> bool:
    return a == b or a in AGREES_WITH.get(b, ()) or b in AGREES_WITH.get(a, ())


def assess(verdict, ask_on: str = "low") -> Review:
    if ask_on not in ASK_LEVELS:
        raise ValueError(f"ask_on must be one of {', '.join(ASK_LEVELS)}")
    if ask_on == "never":
        return Review(False)

    reasons, codes = [], []

    def flag(code, text):
        codes.append(code)
        reasons.append(text)

    fc = verdict.fault_class
    if not verdict.resolved:
        flag("unresolved", "no root cause was found")

    conf = (verdict.confidence or "").lower()
    if verdict.source in ("agent", "llm") and conf:
        if conf == "low" or (ask_on in ("medium", "always") and conf == "medium"):
            flag("self_rated", f"the LLM rated its own confidence {conf}")

    if verdict.rules_agree is False:
        flag("rules_disagree", "the rule book reached a different conclusion than the LLM")

    ml = verdict.ml_prediction
    if verdict.source == "ml" and ml and ml.get("confidence", 1) < ML_DECIDE_SAFE:
        flag("weak_ml_decision", f"the ML classifier decided with only {ml['confidence']:.2f} confidence")
    elif (ml and fc and fc not in ML_CLASSES_EXCLUDED and ml.get("label") not in ML_CLASSES_EXCLUDED
          and ml.get("confidence", 0) >= ML_DISAGREE_MIN and not _compatible(fc, ml["label"])):
        flag("ml_disagrees", f"the ML classifier thinks it is {ml['label']} ({ml['confidence']:.2f}), not {fc}")

    ml_backs = bool(ml and fc and ml.get("confidence", 0) >= ML_DISAGREE_MIN
                    and _compatible(fc, ml.get("label", "")))
    if (verdict.source in ("agent", "llm") and verdict.resolved
            and verdict.rules_agree is None and not ml_backs):
        flag("unsupported", "nothing backs this answer: no rule finding supports it "
                            "and the ML classifier doesn't confidently agree")

    if any("No rule was conclusive" in n for n in verdict.notes):
        flag("tentative_rule", "no rule was conclusive; this is the rule chain's best guess")
    if fc == "other":
        flag("other_class", "the cause is outside the known fault classes")

    if ask_on == "always" and not reasons:
        flag("always", "review requested for every run (--ask-on always)")
    return Review(bool(reasons), reasons, codes)
