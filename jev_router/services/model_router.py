import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from ..config import STRATEGIES, Settings
from ..engine.decision import DecisionEngine
from ..engine.features import RequestFeatures, extract_features, tool_name
from ..engine.filters import filter_models
from ..engine.policy import PolicyEngine, RoutingPlan
from ..providers import ProviderError
from ..registry import ModelSpec, Registry
from ..schemas import ChatCompletionRequest, RouterOptions
from ..storage import TraceStore
from .executor import AllProvidersFailed, Attempt, Executor, attempts_dict
from .guardrails import Guardrails, GuardResult

log = logging.getLogger("jev_router")

AUTO_ALIASES = {"auto", "jev", "jev-router", "jev-router/auto", "router"}
TOOL_QUESTION = "tool_relevance"


class RoutingError(Exception):
    def __init__(self, status: int, message: str, code: str, details: dict[str, Any] | None = None, trace_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.details = details or {}
        self.trace_id = trace_id


@dataclass
class Routed:
    trace_id: str
    started: float
    requested_model: str
    features: RequestFeatures
    plan: RoutingPlan
    chain: list[ModelSpec]
    payload: dict[str, Any]
    explicit: bool = False
    guard: GuardResult | None = None
    tools: dict[str, Any] | None = None
    decision_ms: float = 0.0


def _num(v: Any) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class ModelRouter:
    def __init__(
        self,
        settings: Settings,
        registry: Registry,
        decision: DecisionEngine,
        policy: PolicyEngine,
        executor: Executor,
        store: TraceStore,
        guardrails: Guardrails,
    ):
        self.settings = settings
        self.registry = registry
        self.decision = decision
        self.policy = policy
        self.executor = executor
        self.store = store
        self.guardrails = guardrails
        self._background: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- planning

    def resolve(self, model_field: str, opts: RouterOptions, header_strategy: str | None) -> tuple[str, ModelSpec | None]:
        name = (model_field or "auto").strip()
        base, _, suffix = name.partition(":")
        strategy = opts.strategy or header_strategy or self.settings.default_strategy
        if base.lower() in AUTO_ALIASES:
            strategy = (suffix or strategy).lower()
            explicit = None
        else:
            explicit = self.registry.get_model(name)
            if explicit is None:
                raise RoutingError(404, f"Model '{name}' is not in the registry. Use 'auto' or one of GET /v1/models.", "model_not_found")
            if not self.registry.is_available(explicit):
                raise RoutingError(400, f"Model '{explicit.id}' is disabled or its provider key is not configured.", "model_unavailable")
        if strategy not in STRATEGIES:
            raise RoutingError(400, f"Unknown strategy '{strategy}'. Use one of: {', '.join(STRATEGIES)}.", "invalid_strategy")
        return strategy, explicit

    async def plan(
        self,
        messages: list[dict[str, Any]],
        payload: dict[str, Any],
        opts: RouterOptions,
        model_field: str = "auto",
        header_strategy: str | None = None,
    ) -> Routed:
        started = time.perf_counter()
        trace_id = f"jr-{uuid.uuid4().hex[:20]}"
        strategy, explicit = self.resolve(model_field, opts, header_strategy)
        features = extract_features(messages, payload)

        if explicit:
            eligible, rejected = [explicit], {}
        else:
            eligible, rejected = filter_models(self.registry.available_models(), features, opts)
            if not eligible:
                raise RoutingError(
                    400,
                    "No available model satisfies this request's constraints.",
                    "no_eligible_model",
                    {"rejected": rejected, "available": [m.id for m in self.registry.available_models()]},
                    trace_id,
                )

        extra: dict[str, dict[str, Any]] = {}
        use_guard = opts.guardrails or self.guardrails.cfg.always_on
        if use_guard:
            extra.update(self.guardrails.questions())
        tools = payload.get("tools") or []
        prune = bool(opts.tool_top_k and len(tools) > opts.tool_top_k)
        if prune:
            extra[TOOL_QUESTION] = {
                "type": "choice",
                "instructions": "Which tool is most relevant to fulfilling this request?",
                "criteria": {tool_name(t): (t.get("function") or {}).get("description") or tool_name(t) for t in tools},
            }

        signals = await self.decision.decide(features, eligible, extra or None, use_jev=not explicit or bool(extra))
        if explicit:
            signals.source = "explicit" if not extra else signals.source

        guard = self.guardrails.evaluate(signals.extra) if use_guard else None
        plan = self.policy.rank(eligible, signals, features, strategy, rejected)

        payload = dict(payload)
        tool_info = None
        if prune:
            probs = (signals.extra.get(TOOL_QUESTION) or {}).get("probabilities") or {}
            ordered = sorted(tools, key=lambda t: -probs.get(tool_name(t), 0.0))
            kept = ordered[: opts.tool_top_k]
            payload["tools"] = kept
            tool_info = {
                "kept": [tool_name(t) for t in kept],
                "dropped": [tool_name(t) for t in ordered[opts.tool_top_k :]],
                "probabilities": {k: round(float(v), 4) for k, v in probs.items()},
            }

        chain_len = 1 + (self.settings.max_fallbacks if opts.fallbacks and not explicit else 0)
        routed = Routed(
            trace_id=trace_id,
            started=started,
            requested_model=model_field,
            features=features,
            plan=plan,
            chain=[r.model for r in plan.ranked[:chain_len]],
            payload=payload,
            explicit=bool(explicit),
            guard=guard,
            tools=tool_info,
            decision_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        if guard and self.guardrails.blocks(guard):
            trace = self.build_trace(routed, status="blocked", error="blocked by guardrails")
            await self.store.record(trace)
            raise RoutingError(
                403, "Request blocked by Jev Router guardrails.", "policy_violation", {"guardrails": guard.as_dict()}, trace_id
            )
        return routed

    # ---------------------------------------------------------------- traces

    def build_trace(
        self,
        routed: Routed,
        status: str,
        model: ModelSpec | None = None,
        attempts: list[Attempt] | None = None,
        usage: dict[str, Any] | None = None,
        upstream_ms: float | None = None,
        stream: bool = False,
        error: str | None = None,
    ) -> dict[str, Any]:
        plan, f = routed.plan, routed.features
        s = plan.signals
        model = model or plan.selected.model
        provider = self.registry.provider_for(model).name
        in_tok = int((usage or {}).get("prompt_tokens") or f.input_tokens)
        out_tok = int((usage or {}).get("completion_tokens") or 0)
        estimated = next((r.estimated_cost for r in plan.ranked if r.model.id == model.id), model.estimate_cost(f.input_tokens, f.output_tokens))
        actual = baseline = None
        if usage:
            actual = _num(usage.get("cost"))
            if actual is None:
                actual = model.estimate_cost(in_tok, out_tok)
            baseline = plan.baseline.estimate_cost(in_tok, out_tok) if plan.baseline else actual
            baseline = max(baseline, actual)
        preview = f.prompt[:300] if self.settings.log_prompts else None
        return {
            "id": routed.trace_id,
            "created_at": time.time(),
            "endpoint": "chat",
            "status": status,
            "stream": stream,
            "requested_model": routed.requested_model,
            "strategy": plan.strategy,
            "selected_model": model.id,
            "provider": provider,
            "planned_model": plan.selected.model.id,
            "decision_engine": s.source,
            "jev_model": s.jev_model,
            "task_type": s.task_type,
            "complexity": s.complexity_label,
            "complexity_score": s.complexity,
            "confidence": s.choice_confidence,
            "premium_probability": s.premium_prob,
            "required_level": plan.required_level,
            "low_confidence": plan.low_confidence,
            "routing_reason": plan.reason,
            "model_probabilities": s.model_probs,
            "ranking": [
                {"model": r.model.id, "score": r.score, "estimated_cost": round(r.estimated_cost, 8), "components": r.components}
                for r in plan.ranked
            ],
            "rejected": plan.rejected,
            "fallback_chain": [m.id for m in routed.chain],
            "fallback_used": bool(attempts and len(attempts) > 1),
            "attempts": attempts_dict(attempts or []),
            "guardrails": routed.guard.as_dict() if routed.guard else None,
            "tools": routed.tools,
            "usage": usage,
            "cost": {
                "estimated": round(estimated, 8),
                "actual": round(actual, 8) if actual is not None else None,
                "baseline": round(baseline, 8) if baseline is not None else None,
                "baseline_model": plan.baseline.id if plan.baseline else None,
                "saved": round(baseline - actual, 8) if actual is not None and baseline is not None else None,
            },
            "latency_ms": {
                "decision": routed.decision_ms,
                "jev": s.latency_ms,
                "upstream": upstream_ms,
                "total": round((time.perf_counter() - routed.started) * 1000, 1),
            },
            "prompt_preview": preview,
            "error": error or s.error,
        }

    @staticmethod
    def public_trace(trace: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in trace.items() if k not in ("prompt_preview", "created_at", "endpoint")}

    @staticmethod
    def headers(trace: dict[str, Any]) -> dict[str, str]:
        return {
            "X-Jev-Router-Trace-Id": trace["id"],
            "X-Jev-Router-Model": trace["selected_model"],
            "X-Jev-Router-Strategy": trace["strategy"],
            "X-Jev-Router-Decision": trace["decision_engine"],
            "X-Jev-Router-Complexity": trace["complexity"],
            "X-Jev-Router-Confidence": f"{trace['confidence']:.3f}",
        }

    def _record_later(self, trace: dict[str, Any]) -> None:
        task = asyncio.get_running_loop().create_task(self.store.record(trace))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def drain(self) -> None:
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    # ---------------------------------------------------------------- execution

    @staticmethod
    def upstream_payload(body: ChatCompletionRequest) -> dict[str, Any]:
        payload = body.model_dump(exclude={"jev_router"}, exclude_none=True)
        payload.pop("model", None)
        if not payload.get("tools"):
            payload.pop("tools", None)
        return payload

    async def chat(self, body: ChatCompletionRequest, header_strategy: str | None = None) -> tuple[dict[str, Any], dict[str, str]]:
        opts = body.jev_router or RouterOptions()
        routed = await self.plan(body.messages, self.upstream_payload(body), opts, body.model, header_strategy)
        t0 = time.perf_counter()
        try:
            model, result, attempts = await self.executor.complete(routed.chain, routed.payload)
        except AllProvidersFailed as e:
            trace = self.build_trace(routed, "error", attempts=e.attempts, error=str(e))
            await self.store.record(trace)
            raise RoutingError(e.status if e.status >= 400 else 502, str(e), "upstream_error", {"attempts": attempts_dict(e.attempts)}, routed.trace_id)
        upstream_ms = round((time.perf_counter() - t0) * 1000, 1)
        trace = self.build_trace(routed, "success", model, attempts, result.get("usage") or {}, upstream_ms)
        await self.store.record(trace)
        result["model"] = model.id
        result["jev_router"] = self.public_trace(trace)
        return result, self.headers(trace)

    async def chat_stream(
        self, body: ChatCompletionRequest, header_strategy: str | None = None
    ) -> tuple[AsyncIterator[str], dict[str, str]]:
        opts = body.jev_router or RouterOptions()
        routed = await self.plan(body.messages, self.upstream_payload(body), opts, body.model, header_strategy)
        want_usage = bool((body.model_extra or {}).get("stream_options", {}).get("include_usage"))
        t0 = time.perf_counter()
        try:
            model, first, stream, attempts = await self.executor.open_stream(routed.chain, routed.payload)
        except AllProvidersFailed as e:
            trace = self.build_trace(routed, "error", attempts=e.attempts, stream=True, error=str(e))
            await self.store.record(trace)
            raise RoutingError(e.status if e.status >= 400 else 502, str(e), "upstream_error", {"attempts": attempts_dict(e.attempts)}, routed.trace_id)

        preview = self.build_trace(routed, "streaming", model, attempts, None, None, stream=True)

        async def events() -> AsyncIterator[str]:
            usage: dict[str, Any] | None = None
            out_chars = 0
            status, error = "success", None
            chunk: dict[str, Any] | None = first
            is_first = True
            try:
                while chunk is not None:
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        out_chars += len(delta.get("content") or "") + len(delta.get("reasoning_content") or "")
                    chunk["model"] = model.id
                    if chunk.get("choices") or want_usage:
                        if is_first:
                            chunk["jev_router"] = self.public_trace(preview)
                            is_first = False
                        yield f"data: {json.dumps(chunk)}\n\n"
                    chunk = await anext(stream, None)
                yield "data: [DONE]\n\n"
            except ProviderError as e:
                status, error = "error", f"stream interrupted: {e.message}"
                yield f"data: {json.dumps({'error': {'message': error, 'type': 'upstream_error', 'code': e.status}})}\n\n"
            except (asyncio.CancelledError, GeneratorExit):
                status, error = "cancelled", "client disconnected"
                raise
            finally:
                final_usage = usage or {"prompt_tokens": routed.features.input_tokens, "completion_tokens": out_chars // 4, "estimated": True}
                upstream_ms = round((time.perf_counter() - t0) * 1000, 1)
                self._record_later(self.build_trace(routed, status, model, attempts, final_usage, upstream_ms, True, error))

        return events(), self.headers(preview)
