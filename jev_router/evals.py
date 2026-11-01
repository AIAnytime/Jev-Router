"""Offline routing evaluation: runs a labelled prompt set through the decision pipeline (no LLM calls)."""

import asyncio
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .engine.cache import TTLCache
from .engine.decision import DecisionEngine
from .engine.features import extract_features
from .engine.filters import filter_models
from .engine.jev_client import JevClient
from .engine.policy import PolicyEngine
from .registry import ModelSpec, Registry
from .schemas import RouterOptions


@dataclass
class Case:
    prompt: str
    tier: str
    task_type: str | None = None


@dataclass
class Outcome:
    case: Case
    selected: str
    tier: str
    task_type: str
    complexity: str
    cost: float
    baseline_cost: float
    decision_ms: float
    source: str


def load_cases(path: str | Path) -> list[Case]:
    cases = []
    for line in Path(path).read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            cases.append(Case(row["prompt"], row["tier"], row.get("task_type")))
    return cases


def tier_of(model: ModelSpec, eligible: list[ModelSpec]) -> str:
    """'heavy' if the model sits in the upper half of the eligible quality range."""
    qs = [m.quality for m in eligible]
    return "heavy" if model.quality >= (min(qs) + max(qs)) / 2 and max(qs) > min(qs) else "light"


def _p(values: list[float], q: int) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=100, method="inclusive")[q - 1]


class Evaluator:
    def __init__(self, settings: Settings, registry: Registry, strategy: str, concurrency: int = 4):
        self.settings = settings
        self.registry = registry
        self.strategy = strategy
        self.policy = PolicyEngine(registry.router, settings.confidence_threshold)
        self.jev = JevClient(settings.jev_api_key, settings.jev_base_url, settings.jev_model, settings.jev_timeout)
        self.engine = DecisionEngine(self.jev, TTLCache(0, 0))
        self.sem = asyncio.Semaphore(concurrency)

    async def run_one(self, case: Case, use_jev: bool) -> Outcome:
        messages = [{"role": "user", "content": case.prompt}]
        features = extract_features(messages, {"messages": messages})
        eligible, rejected = filter_models(self.registry.available_models(), features, RouterOptions())
        async with self.sem:
            signals = await self.engine.decide(features, eligible, use_jev=use_jev)
        plan = self.policy.rank(eligible, signals, features, self.strategy, rejected)
        top = plan.selected
        return Outcome(
            case=case,
            selected=top.model.id,
            tier=tier_of(top.model, eligible),
            task_type=signals.task_type,
            complexity=signals.complexity_label,
            cost=top.estimated_cost,
            baseline_cost=plan.baseline_cost,
            decision_ms=signals.latency_ms or 0.0,
            source=signals.source,
        )

    async def run(self, cases: list[Case], use_jev: bool) -> list[Outcome]:
        return await asyncio.gather(*(self.run_one(c, use_jev) for c in cases))

    async def aclose(self) -> None:
        await self.jev.aclose()


def summarize(outcomes: list[Outcome]) -> dict[str, Any]:
    total = len(outcomes)
    tier_ok = sum(o.tier == o.case.tier for o in outcomes)
    labelled = [o for o in outcomes if o.case.task_type]
    task_ok = sum(o.task_type == o.case.task_type for o in labelled)
    under = sum(o.case.tier == "heavy" and o.tier == "light" for o in outcomes)
    over = sum(o.case.tier == "light" and o.tier == "heavy" for o in outcomes)
    cost = sum(o.cost for o in outcomes)
    baseline = sum(o.baseline_cost for o in outcomes)
    lat = [o.decision_ms for o in outcomes if o.decision_ms]
    sources: dict[str, int] = {}
    for o in outcomes:
        sources[o.source] = sources.get(o.source, 0) + 1
    return {
        "cases": total,
        "tier_accuracy": round(tier_ok / total, 4) if total else None,
        "task_accuracy": round(task_ok / len(labelled), 4) if labelled else None,
        "under_routed": under,
        "over_routed": over,
        "estimated_cost": round(cost, 6),
        "always_strongest_cost": round(baseline, 6),
        "cost_saved_pct": round((baseline - cost) / baseline * 100, 1) if baseline else None,
        "decision_ms_p50": round(_p(lat, 50), 1) if lat else None,
        "decision_ms_p95": round(_p(lat, 95), 1) if lat else None,
        "decision_sources": sources,
    }


def _print_report(strategy: str, models: list[str], results: dict[str, dict[str, Any]], misses: list[Outcome]) -> None:
    print(f"\nJev Router eval  ·  strategy={strategy}  ·  models: {', '.join(models)}\n")
    cols = list(results)
    rows = [
        ("Cases", "cases", "{}"),
        ("Tier accuracy", "tier_accuracy", "{:.1%}"),
        ("Task-type accuracy", "task_accuracy", "{:.1%}"),
        ("Under-routed (heavy→light)", "under_routed", "{}"),
        ("Over-routed (light→heavy)", "over_routed", "{}"),
        ("Est. cost", "estimated_cost", "${:.6f}"),
        ("Always-strongest cost", "always_strongest_cost", "${:.6f}"),
        ("Cost saved", "cost_saved_pct", "{}%"),
        ("Decision p50 (ms)", "decision_ms_p50", "{}"),
        ("Decision p95 (ms)", "decision_ms_p95", "{}"),
    ]
    width = max(len(r[0]) for r in rows) + 2
    print("".ljust(width) + "".join(c.rjust(14) for c in cols))
    for label, key, fmt in rows:
        cells = []
        for c in cols:
            v = results[c].get(key)
            cells.append(("—" if v is None else fmt.format(v)).rjust(14))
        print(label.ljust(width) + "".join(cells))
    if misses:
        print("\nJev tier misses:")
        for o in misses:
            print(f"  expected {o.case.tier:<5} got {o.selected} ({o.complexity}, {o.task_type}): {o.case.prompt[:80]}")
    print()


async def run_eval(
    dataset: str, strategy: str, settings: Settings, compare: bool = True, as_json: bool = False, concurrency: int = 4
) -> dict[str, Any]:
    registry = Registry.load(settings.jev_router_config_dir)
    cases = load_cases(dataset)
    ev = Evaluator(settings, registry, strategy, concurrency)
    try:
        results: dict[str, dict[str, Any]] = {}
        jev_out: list[Outcome] = []
        if ev.jev.configured:
            jev_out = await ev.run(cases, use_jev=True)
            results["jev"] = summarize(jev_out)
        if compare or not ev.jev.configured:
            results["heuristic"] = summarize(await ev.run(cases, use_jev=False))
    finally:
        await ev.aclose()
    models = [m.id for m in registry.available_models()]
    if as_json:
        print(json.dumps({"strategy": strategy, "models": models, "results": results}, indent=2))
    else:
        _print_report(strategy, models, results, [o for o in jev_out if o.tier != o.case.tier])
    return results
