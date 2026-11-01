import math
from dataclasses import dataclass, field

from ..registry import ModelSpec, RouterConfig
from .features import RequestFeatures
from .heuristic import Signals, required_level

JEV_SOURCES = {"jev", "cache"}

_COMPONENT_REASON = {
    "fit": "best fit according to Jev",
    "capability": "smallest model that fully meets the required capability",
    "quality": "highest-quality eligible model",
    "cost": "cheapest model that can handle it",
    "latency": "fastest capable model",
    "local": "runs locally, keeping data private",
}


@dataclass
class RankedModel:
    model: ModelSpec
    score: float
    components: dict[str, float]
    estimated_cost: float


@dataclass
class RoutingPlan:
    strategy: str
    signals: Signals
    ranked: list[RankedModel]
    rejected: dict[str, str] = field(default_factory=dict)
    required_level: float = 0.0
    low_confidence: bool = False
    reason: str = ""
    baseline: ModelSpec | None = None
    baseline_cost: float = 0.0

    @property
    def selected(self) -> RankedModel:
        return self.ranked[0]


def _cheapness(costs: dict[str, float]) -> dict[str, float]:
    logs = {k: math.log10(v + 1e-9) for k, v in costs.items()}
    lo, hi = min(logs.values()), max(logs.values())
    if hi - lo < 1e-9:
        return {k: 1.0 for k in logs}
    return {k: 1 - (v - lo) / (hi - lo) for k, v in logs.items()}


class PolicyEngine:
    """Blends Jev's typed signals with the strategy's quality/cost/latency objectives."""

    def __init__(self, cfg: RouterConfig, confidence_threshold: float = 0.55):
        self.cfg = cfg
        self.threshold = confidence_threshold

    def rank(
        self,
        eligible: list[ModelSpec],
        signals: Signals,
        features: RequestFeatures,
        strategy: str,
        rejected: dict[str, str] | None = None,
    ) -> RoutingPlan:
        weights = self.cfg.strategies[strategy]
        from_jev = signals.source in JEV_SOURCES
        confidence = signals.choice_confidence if from_jev else 0.5
        low_conf = from_jev and len(eligible) > 1 and confidence < self.threshold

        level = required_level(signals.complexity, signals.premium_prob)
        if low_conf:
            level = min(1.0, level + 0.1)
        fit_weight = weights.get("fit", 0) * min(1.0, confidence / self.threshold if self.threshold else 1.0)

        costs = {m.id: m.estimate_cost(features.input_tokens, features.output_tokens) for m in eligible}
        cheap = _cheapness(costs)
        max_prob = max(signals.model_probs.values(), default=0.0) or 1.0

        ranked: list[RankedModel] = []
        for m in eligible:
            q = m.quality / 10
            comp = {
                "fit": signals.model_probs.get(m.id, 0.0) / max_prob,
                "capability": 1.0 if q >= level else max(0.0, 1 - (level - q) * 2.5),
                "quality": q,
                "cost": cheap[m.id],
                "latency": m.speed / 10,
                "local": 1.0 if m.local else 0.0,
            }
            w = dict(weights, fit=fit_weight)
            score = sum(w.get(k, 0.0) * v for k, v in comp.items())
            ranked.append(RankedModel(m, round(score, 4), {k: round(v, 3) for k, v in comp.items()}, costs[m.id]))

        capable = [r for r in ranked if r.components["capability"] >= self.cfg.min_capability]
        ranked = capable or ranked
        ranked.sort(key=lambda r: (-r.score, r.estimated_cost))

        baseline = max(eligible, key=lambda m: (m.quality, costs[m.id]))
        plan = RoutingPlan(
            strategy=strategy,
            signals=signals,
            ranked=ranked,
            rejected=rejected or {},
            required_level=round(level, 3),
            low_confidence=low_conf,
            baseline=baseline,
            baseline_cost=costs[baseline.id],
        )
        plan.reason = self._reason(plan, dict(weights, fit=fit_weight), confidence, from_jev)
        return plan

    @staticmethod
    def _reason(plan: RoutingPlan, weights: dict[str, float], confidence: float, from_jev: bool) -> str:
        s, top = plan.signals, plan.selected
        contrib = {k: weights.get(k, 0) * v for k, v in top.components.items()}
        driver = max(contrib, key=contrib.get)
        parts = [f"{s.task_type} request, {s.complexity_label} complexity"]
        if from_jev:
            parts[0] += f" (Jev {confidence:.0%} confident)"
        if len(plan.ranked) == 1:
            parts.append(f"{top.model.id} is the only eligible model")
        else:
            parts.append(f"{plan.strategy} strategy chose {top.model.id}: {_COMPONENT_REASON[driver]}")
        if plan.low_confidence:
            parts.append("Jev confidence below threshold, biased toward stronger models")
        if s.source == "heuristic_fallback":
            parts.append("Jev unavailable, heuristic routing used")
        return "; ".join(parts)
