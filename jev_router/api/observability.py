from fastapi import APIRouter, Depends, Query, Request

from .. import __version__
from ..schemas import FeedbackRequest
from .deps import APIError, authenticate

router = APIRouter(tags=["observability"])


@router.get("/health")
async def health(request: Request):
    state = request.app.state
    available = state.registry.available_models()
    return {
        "status": "ok",
        "version": __version__,
        "jev_configured": state.jev.configured,
        "available_models": [m.id for m in available],
        "providers": sorted({m.provider for m in available}),
        "auth_enabled": bool(state.settings.api_keys),
        "default_strategy": state.settings.default_strategy,
        "decision_cache": state.decision.cache.stats(),
    }


@router.get("/v1/traces")
async def list_traces(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    model: str | None = None,
    status: str | None = None,
    endpoint: str | None = None,
    q: str | None = Query(None, max_length=200),
    _: str = Depends(authenticate),
):
    store = request.app.state.store
    filters = {"model": model, "status": status, "endpoint": endpoint, "q": q}
    return {"data": await store.list(limit, offset, **filters), "total": await store.count(**filters)}


@router.get("/v1/traces/{trace_id}")
async def get_trace(trace_id: str, request: Request, _: str = Depends(authenticate)):
    trace = await request.app.state.store.get(trace_id)
    if not trace:
        raise APIError(404, f"Trace '{trace_id}' not found.", "not_found")
    return trace


@router.post("/v1/traces/{trace_id}/feedback")
async def trace_feedback(trace_id: str, body: FeedbackRequest, request: Request, _: str = Depends(authenticate)):
    if not await request.app.state.store.set_feedback(trace_id, body.score, body.comment):
        raise APIError(404, f"Trace '{trace_id}' not found.", "not_found")
    return {"id": trace_id, "feedback": body.score, "comment": body.comment}


@router.get("/v1/analytics")
async def analytics(request: Request, hours: float = Query(24, gt=0, le=24 * 90), _: str = Depends(authenticate)):
    return await request.app.state.store.analytics(hours)
