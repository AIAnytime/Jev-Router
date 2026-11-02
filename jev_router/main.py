import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .api import chat, observability, routing, settings as settings_api
from .api.deps import APIError, RateLimiter, error_response
from .config import Settings, get_settings
from .crypto import SecretBox
from .engine.cache import TTLCache
from .engine.decision import DecisionEngine
from .engine.jev_client import JevClient
from .engine.policy import PolicyEngine
from .providers import build_providers
from .registry import Registry
from .services.executor import Executor
from .services.guardrails import Guardrails
from .services.model_router import ModelRouter, RoutingError
from .services.settings_service import SettingsService
from .services.task_routers import AgentRouter, ToolRouter
from .settings_store import SettingsStore
from .storage import TraceStore

DASHBOARD = Path(__file__).parent / "dashboard" / "index.html"
log = logging.getLogger("jev_router")


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        registry = Registry.load(settings.jev_router_config_dir)
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
            transport=transport,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
        )
        jev = JevClient(settings.jev_api_key, settings.jev_base_url, settings.jev_model, settings.jev_timeout, transport)
        store = TraceStore(settings.database_url)
        await store.init()
        decision = DecisionEngine(jev, TTLCache(settings.decision_cache_size, settings.decision_cache_ttl))
        box = SecretBox.from_settings(settings.jev_router_secret, settings.jev_router_secret_file)
        settings_service = SettingsService(registry, SettingsStore(store.engine), box, decision.cache)
        await settings_service.load()
        for warning in settings_service.warnings:
            log.warning("settings: %s", warning)
        guardrails = Guardrails(registry.router.guardrails)

        s = app.state
        s.settings, s.registry, s.jev, s.store, s.decision, s.guardrails = settings, registry, jev, store, decision, guardrails
        s.limiter = RateLimiter(settings.rate_limit_rpm)
        s.http, s.settings_service = http, settings_service
        s.model_router = ModelRouter(
            settings, registry, decision, PolicyEngine(registry.router, settings.confidence_threshold),
            Executor(registry, build_providers(http)), store, guardrails,
        )
        s.agent_router = AgentRouter(jev, registry.agents)
        s.tool_router = ToolRouter(jev)

        available = registry.available_models()
        log.info("Jev Router %s: %d models available (%s); Jev %s", __version__, len(available),
                 ", ".join(m.id for m in available) or "none", "configured" if jev.configured else "NOT configured")
        if not settings.api_keys:
            log.warning("JEV_ROUTER_API_KEYS is empty: the router accepts unauthenticated requests")
        yield
        await s.model_router.drain()
        await http.aclose()
        await jev.aclose()
        await store.close()

    app = FastAPI(
        title="Jev Router",
        version=__version__,
        description="Decision-first, OpenAI-compatible AI router powered by Jev typed decisions.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Jev-Router-Trace-Id", "X-Jev-Router-Model", "X-Jev-Router-Strategy",
                        "X-Jev-Router-Decision", "X-Jev-Router-Complexity", "X-Jev-Router-Confidence"],
    )

    @app.exception_handler(APIError)
    async def _api_error(_: Request, e: APIError):
        resp = error_response(e.status, e.message, e.code, e.type, **e.extra)
        if e.status == 429 and e.extra.get("retry_after"):
            resp.headers["Retry-After"] = str(e.extra["retry_after"])
        return resp

    @app.exception_handler(RoutingError)
    async def _routing_error(_: Request, e: RoutingError):
        type_ = "upstream_error" if e.code == "upstream_error" else "invalid_request_error"
        resp = error_response(e.status, e.message, e.code, type_, details=e.details or None, trace_id=e.trace_id)
        if e.trace_id:
            resp.headers["X-Jev-Router-Trace-Id"] = e.trace_id
        return resp

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        loc = ".".join(str(p) for p in first.get("loc", []) if p != "body")
        return error_response(400, f"{loc}: {first.get('msg', 'invalid request')}".strip(": "), "invalid_request")

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, e: StarletteHTTPException):
        return error_response(e.status_code, str(e.detail), "http_error")

    app.include_router(chat.router)
    app.include_router(routing.router)
    app.include_router(observability.router)
    app.include_router(settings_api.router)

    @app.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(DASHBOARD, headers={"Cache-Control": "no-cache"})

    return app


app = create_app()
