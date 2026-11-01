from dataclasses import dataclass, field
from typing import Any

from ..registry import GuardrailConfig

PREFIX = "guard_"


@dataclass
class GuardResult:
    verdict: str
    checks: dict[str, float]
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"verdict": self.verdict, "checks": self.checks, "reasons": self.reasons}


class Guardrails:
    """Policy router: turns Jev yes/no probabilities into allow / review / deny verdicts."""

    def __init__(self, cfg: GuardrailConfig):
        self.cfg = cfg

    def questions(self, action: str | None = None) -> dict[str, dict[str, Any]]:
        qs = {}
        for name, check in self.cfg.checks.items():
            instructions = check.instructions
            if action and name == "needs_approval":
                instructions += f" The intended action is: {action}"
            qs[PREFIX + name] = {"type": "noul", "instructions": instructions}
        return qs

    def evaluate(self, answers: dict[str, Any]) -> GuardResult:
        checks: dict[str, float] = {}
        reasons: list[str] = []
        verdict = "allow"
        for name, check in self.cfg.checks.items():
            answer = answers.get(PREFIX + name)
            if not answer or "noul" not in answer:
                continue
            p = round(float(answer["noul"]), 4)
            checks[name] = p
            if check.deny is not None and p >= check.deny:
                verdict = "deny"
                reasons.append(f"{name} {p:.2f} >= deny threshold {check.deny}")
            elif check.review is not None and p >= check.review:
                if verdict == "allow":
                    verdict = "review"
                reasons.append(f"{name} {p:.2f} >= review threshold {check.review}")
        return GuardResult(verdict, checks, reasons)

    def blocks(self, result: GuardResult) -> bool:
        return result.verdict == "deny" or (result.verdict == "review" and self.cfg.block_on_review)
