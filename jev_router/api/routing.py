import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Request

from ..engine.features import truncate
from ..engine.jev_client import JevError
from ..schemas import RouteAgentRequest, RouteModelRequest, RoutePolicyRequest, RouteToolRequest
from .deps import APIError, authenticate

router = APIRouter(prefix="/v1/route", tags=["routing"])


async def _record(request: Request, endpoint: str, result: dict[str, Any], selected: str | None, preview: str) -> str:
    trace_id = f"jr-{uuid.uuid4().hex[:20]}"
    settings = request.app.state.settings
    await request.app.state.model_router.record({
        "id": trace_id,
        "created_at": time.time(),
        "endpoint": endpoint,
        "status": "success",
        "selected_model": selected,
        "decision_engine": result.get("decision_engine"),
        "complexity": result.get("complexity"),
        "confidence": result.get("confidence"),
        "latency_ms": {"total": result.get("latency_ms"), "decision": result.get("latency_ms")},
        "prompt_preview": preview[:300] if settings.log_prompts else None,
        "result": result,
    })
    return trace_id


@router.post("/model")
async def route_model(body: RouteModelRequest, request: Request, _: str = Depends(authenticate)):
    """Dry run: returns the full routing decision without calling any LLM."""
    try:
        messages = body.to_messages()
    except ValueError as e:
        raise APIError(400, str(e), "invalid_request")
    payload: dict[str, Any] = {"messages": messages}
    for key in ("max_tokens", "tools", "response_format"):
        if getattr(body, key):
            payload[key] = getattr(body, key)
    svc = request.app.state.model_router
    routed = await svc.plan(messages, payload, body.jev_router, body.model, request.headers.get("x-jev-strategy"))
    trace = svc.build_trace(routed, "planned")
    trace["endpoint"] = "route_model"
    await svc.record(trace)
    return svc.public_trace(trace)


@router.post("/agent")
async def route_agent(body: RouteAgentRequest, request: Request, _: str = Depends(authenticate)):
    try:
        result = await request.app.state.agent_router.route(body)
    except ValueError as e:
        raise APIError(400, str(e), "invalid_request")
    result["id"] = await _record(request, "route_agent", result, result["agent"], body.task)
    return result


@router.post("/tool")
async def route_tool(body: RouteToolRequest, request: Request, _: str = Depends(authenticate)):
    try:
        result = await request.app.state.tool_router.route(body)
    except ValueError as e:
        raise APIError(400, str(e), "invalid_request")
    result["id"] = await _record(request, "route_tool", result, result["tool"], body.task or "")
    return result


@router.post("/policy")
async def route_policy(body: RoutePolicyRequest, request: Request, _: str = Depends(authenticate)):
    """Guardrail check: allow / review / deny with per-check probabilities."""
    guardrails = request.app.state.guardrails
    jev = request.app.state.jev
    start = time.perf_counter()
    state: dict[str, Any] = {"content": truncate(body.content, 12000)}
    if body.action:
        state["intended_action"] = body.action
    if body.context:
        state["context"] = body.context
    try:
        data = await jev.ask(state, guardrails.questions(body.action))
        g = guardrails.evaluate(data["answers"])
        result = {**g.as_dict(), "decision_engine": "jev"}
    except JevError as e:
        # Fail closed: without Jev we cannot judge the content, so ask for human review.
        result = {"verdict": "review", "checks": {}, "reasons": [f"Jev unavailable: {e}"], "decision_engine": "unavailable"}
    result["latency_ms"] = round((time.perf_counter() - start) * 1000, 1)
    result["id"] = await _record(request, "route_policy", result, result["verdict"], body.content)
    return result
