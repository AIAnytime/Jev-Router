from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..schemas import ChatCompletionRequest
from .deps import authenticate

router = APIRouter(prefix="/v1", tags=["chat"])


@router.post("/chat/completions")
async def chat_completions(body: ChatCompletionRequest, request: Request, _: str = Depends(authenticate)):
    """OpenAI-compatible chat completions. Use model="auto" (or "auto:<strategy>") to let Jev route."""
    svc = request.app.state.model_router
    strategy = request.headers.get("x-jev-strategy")
    if body.stream:
        events, headers = await svc.chat_stream(body, strategy)
        return StreamingResponse(
            events, media_type="text/event-stream",
            headers={**headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    result, headers = await svc.chat(body, strategy)
    return JSONResponse(result, headers=headers)


@router.get("/models")
async def list_models(request: Request, _: str = Depends(authenticate)):
    registry = request.app.state.registry
    strategies = request.app.state.registry.router.strategies
    data = [
        {"id": "auto", "object": "model", "owned_by": "jev-router", "description": "Jev-routed, default strategy"},
        *[
            {"id": f"auto:{s}", "object": "model", "owned_by": "jev-router", "description": f"Jev-routed, {s} strategy"}
            for s in strategies
        ],
    ]
    for m in registry.models:
        provider = registry.provider_for(m)
        data.append({
            "id": m.id,
            "object": "model",
            "owned_by": m.provider,
            "available": registry.is_available(m),
            "requires_key": provider.requires_key,
            "enable_env": provider.api_key_env if provider.requires_key else provider.enabled_env,
            "upstream_model": m.model,
            "description": m.description,
            "quality": m.quality,
            "speed": m.speed,
            "pricing": {"input_per_1m": m.input_cost, "output_per_1m": m.output_cost},
            "context_window": m.context_window,
            "modalities": m.modalities,
            "supports_tools": m.supports_tools,
            "local": m.local,
        })
    return {"object": "list", "data": data}
