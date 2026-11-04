import dataclasses
import hashlib
import json
import logging
import time
from typing import Any

from ..registry import ModelSpec
from .cache import TTLCache
from .features import RequestFeatures, truncate
from .heuristic import (
    COMPLEXITY_CRITERIA,
    COMPLEXITY_LABELS,
    TASK_TYPES,
    Signals,
    heuristic_signals,
)
from .jev_client import JevClient, JevError

log = logging.getLogger("jev_router.decision")

BASE_QUESTIONS = 4
PROMPT_CHARS = 8000


def build_state(features: RequestFeatures, eligible: list[ModelSpec]) -> dict[str, Any]:
    request: dict[str, Any] = {"prompt": truncate(features.prompt, PROMPT_CHARS)}
    if features.system:
        request["system_prompt"] = truncate(features.system, 1500)
    if features.history:
        request["recent_conversation"] = truncate(features.history, 2000)
    request["conversation_turns"] = features.turns
    request["estimated_input_tokens"] = features.input_tokens
    if features.needs:
        request["requires"] = sorted(features.needs)
    if features.tool_names:
        request["tools_available"] = features.tool_names[:25]
    state: dict[str, Any] = {"request": request}
    if eligible:
        state["available_models"] = {m.id: m.jev_description() for m in eligible}
    return state


def routing_questions(eligible: list[ModelSpec]) -> dict[str, dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {}
    if len(eligible) > 1:
        questions["model_route"] = {
            "type": "choice",
            "instructions": (
                "Which available model is the best fit for this specific request, considering its task, "
                "difficulty and each model's strengths? A stronger model is only a better fit when the "
                "request actually needs that extra capability."
            ),
            "criteria": {m.id: m.jev_description() for m in eligible},
        }
    questions["complexity"] = {
        "type": "score",
        "instructions": "Rate how technically demanding it is to answer this request well.",
        "criteria": COMPLEXITY_CRITERIA,
    }
    questions["premium_required"] = {
        "type": "noul",
        "instructions": (
            "Does answering this request well genuinely require a premium, frontier-level reasoning model "
            "(deep multi-step reasoning, hard or large-scale code, rigorous math, expert analysis)?"
        ),
    }
    questions["task_type"] = {
        "type": "choice",
        "instructions": "What kind of task is this request?",
        "criteria": TASK_TYPES,
    }
    return questions


def label_from_score(answer: dict[str, Any]) -> str:
    probs = answer.get("probabilities") or {}
    if probs:
        idx = int(max(probs, key=lambda k: probs[k]))
    else:
        idx = round(float(answer.get("score", 1)))
    return COMPLEXITY_LABELS[max(0, min(3, idx))]


def parse_answers(
    data: dict[str, Any], eligible: list[ModelSpec], fallback: Signals
) -> Signals:
    answers = data.get("answers") or {}
    ids = {m.id for m in eligible}

    route = answers.get("model_route")
    if route:
        probs = {k: float(v) for k, v in (route.get("probabilities") or {}).items() if k in ids}
        choice = route.get("choice") if route.get("choice") in ids else None
        if not choice and probs:
            choice = max(probs, key=probs.get)
        confidence = float(route.get("confidence", probs.get(choice, 0.0) if choice else 0.0))
        if not probs and choice:
            probs = {choice: 1.0}
    else:
        only = eligible[0].id if eligible else None
        probs, choice, confidence = ({only: 1.0} if only else {}), only, 1.0

    cx = answers.get("complexity") or {}
    task = answers.get("task_type") or {}
    premium = answers.get("premium_required") or {}
    extra = {k: v for k, v in answers.items() if k not in ("model_route", "complexity", "premium_required", "task_type")}

    return Signals(
        source="jev",
        model_probs={k: round(v, 4) for k, v in probs.items()},
        choice=choice,
        choice_confidence=round(confidence, 4),
        complexity=round(float(cx["score"]), 3) if "score" in cx else fallback.complexity,
        complexity_label=label_from_score(cx) if cx else fallback.complexity_label,
        complexity_confidence=float(cx.get("confidence", 0.0)) if cx else 0.0,
        premium_prob=round(float(premium["noul"]), 4) if "noul" in premium else fallback.premium_prob,
        task_type=task.get("choice") if task.get("choice") in TASK_TYPES else fallback.task_type,
        task_confidence=float(task.get("confidence", 0.0)),
        extra=extra,
        jev_model=data.get("model"),
        usage=data.get("usage"),
    )


class DecisionEngine:
    """Asks Jev for typed routing signals in a single call; degrades to heuristics if Jev is unavailable."""

    def __init__(self, jev: JevClient | None, cache: TTLCache):
        self.jev = jev
        self.cache = cache

    def _cache_key(self, features: RequestFeatures, eligible: list[ModelSpec], extra: dict | None) -> str:
        raw = json.dumps(
            [features.cache_key_parts(), sorted(m.id for m in eligible), sorted((extra or {}).items())],
            default=str,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    async def decide(
        self,
        features: RequestFeatures,
        eligible: list[ModelSpec],
        extra_questions: dict[str, dict[str, Any]] | None = None,
        use_jev: bool = True,
    ) -> Signals:
        fallback = heuristic_signals(features, eligible)
        if not use_jev or self.jev is None or not self.jev.configured:
            return dataclasses.replace(fallback, error=None if not use_jev else "jev not configured")
        if len(eligible) == 1 and not extra_questions:
            return dataclasses.replace(
                fallback, source="single_candidate", model_probs={eligible[0].id: 1.0},
                choice=eligible[0].id, choice_confidence=1.0,
            )

        key = self._cache_key(features, eligible, extra_questions)
        cached = self.cache.get(key)
        if cached is not None:
            return dataclasses.replace(cached, source="cache", latency_ms=0.0)

        questions = routing_questions(eligible)
        for name, q in (extra_questions or {}).items():
            if len(questions) >= JevClient.MAX_QUESTIONS:
                break
            questions[name] = q

        start = time.perf_counter()

        def heuristic(error: str) -> Signals:
            log.warning("Jev decision failed, using heuristic router: %s", error)
            return dataclasses.replace(
                fallback, source="heuristic_fallback", error=error,
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
            )

        try:
            data = await self.jev.ask(build_state(features, eligible), questions)
        except JevError as e:
            return heuristic(str(e))
        try:
            signals = parse_answers(data, eligible, fallback)
        except (TypeError, ValueError, KeyError, AttributeError) as e:
            # Jev answered, but with values we cannot use (null confidence, non-numeric labels, ...).
            return heuristic(f"Jev returned malformed answers: {e.__class__.__name__}")
        signals.latency_ms = round((time.perf_counter() - start) * 1000, 1)
        self.cache.set(key, signals)
        return signals
