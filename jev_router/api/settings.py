import hmac
import ipaddress
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..crypto import mask
from ..registry import ModelSpec, ProviderSpec
from ..services.discovery import DiscoveryError, list_models, probe
from ..services.settings_service import SettingsService
from .deps import APIError, _extract_key

LOOPBACK = {"127.0.0.1", "::1"}
NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
PRINTABLE_PATTERN = r"^[\x20-\x7e]*$"
PRINTABLE = re.compile(PRINTABLE_PATTERN)
NULLABLE_MODEL_FIELDS = {"max_output_tokens"}


async def require_admin(request: Request) -> None:
    admin_key = request.app.state.settings.jev_router_admin_key
    if admin_key:
        supplied = _extract_key(request) or ""
        if hmac.compare_digest(supplied.encode(), admin_key.encode()):
            return
        raise APIError(403, "A valid admin key is required for Settings.", "settings_forbidden", "permission_error")
    host = request.client.host if request.client else ""
    proxied = "x-forwarded-for" in request.headers or "forwarded" in request.headers
    if host in LOOPBACK and not proxied:
        return
    raise APIError(403, "Settings can only be managed from this machine. Set JEV_ROUTER_ADMIN_KEY to manage remotely.",
                   "settings_forbidden", "permission_error")


router = APIRouter(prefix="/v1/settings", tags=["settings"], dependencies=[Depends(require_admin)])


def clean_url(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http or https URL with a host")
    return value.rstrip("/")


def clean_headers(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if len(value) > 20:
        raise ValueError("at most 20 headers are allowed")
    for name, v in value.items():
        if not HEADER_NAME.match(name):
            raise ValueError(f"invalid header name '{name[:64]}'")
        if name.lower() == "authorization":
            raise ValueError("set api_key instead of an Authorization header")
        if len(v) > 2048:
            raise ValueError(f"header '{name}' is longer than 2048 characters")
        if not PRINTABLE.match(v):
            raise ValueError(f"header '{name}' must be printable ASCII")
    return value


def is_local_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host in ("localhost", "host.docker.internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


Secret = Annotated[str, Field(min_length=1, max_length=4096, pattern=PRINTABLE_PATTERN)]
Modality = Literal["text", "image", "audio"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("base_url", check_fields=False)
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return v if v is None else clean_url(v)

    @field_validator("headers", check_fields=False)
    @classmethod
    def _headers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return clean_headers(v)


class ProviderCreate(Body):
    name: str = Field(pattern=NAME_PATTERN)
    type: Literal["openai", "anthropic"] = "openai"
    base_url: str
    api_key: Secret | None = None
    headers: dict[str, str] | None = None
    local: bool | None = None
    enabled: bool = True


class ProviderPatch(Body):
    type: Literal["openai", "anthropic"] | None = None
    base_url: str | None = None
    api_key: Secret | None = None
    headers: dict[str, str] | None = None
    local: bool | None = None
    enabled: bool | None = None


class ProviderProbe(Body):
    type: Literal["openai", "anthropic"] = "openai"
    base_url: str
    api_key: Secret | None = None
    headers: dict[str, str] | None = None


class SavedProviderProbe(Body):
    base_url: str | None = None
    api_key: Secret | None = None


class ModelFields(Body):
    description: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    strengths: Annotated[list[Annotated[str, Field(max_length=60)]], Field(max_length=20)] | None = None
    quality: Annotated[float, Field(ge=1, le=10)] | None = None
    speed: Annotated[float, Field(ge=1, le=10)] | None = None
    input_cost: Annotated[float, Field(ge=0)] | None = None
    output_cost: Annotated[float, Field(ge=0)] | None = None
    context_window: Annotated[int, Field(gt=0)] | None = None
    max_output_tokens: Annotated[int, Field(gt=0)] | None = None
    modalities: list[Modality] | None = None
    only_when: list[Literal["image", "audio"]] | None = None
    supports_tools: bool | None = None
    supports_json: bool | None = None
    reasoning: bool | None = None
    enabled: bool | None = None


class ModelCreate(ModelFields):
    provider: str = Field(pattern=NAME_PATTERN)
    model: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")


def _svc(request: Request) -> SettingsService:
    return request.app.state.settings_service


def _provider_or_404(request: Request, name: str) -> ProviderSpec:
    p = request.app.state.registry.providers.get(name)
    if p is None:
        raise APIError(404, f"Provider '{name}' not found.", "not_found")
    return p


def _model_or_404(request: Request, model_id: str) -> ModelSpec:
    m = next((m for m in request.app.state.registry.models if m.id == model_id), None)
    if m is None:
        raise APIError(404, f"Model '{model_id}' not found.", "not_found")
    return m


def _reject_nulls(changes: dict[str, Any], allowed: set[str]) -> None:
    if not changes:
        raise APIError(400, "No changes supplied.", "invalid_request")
    nulls = sorted(k for k, v in changes.items() if v is None and k not in allowed)
    if nulls:
        raise APIError(400, f"{nulls[0]}: may not be null", "invalid_request")


def provider_view(request: Request, p: ProviderSpec) -> dict[str, Any]:
    models = [m for m in request.app.state.registry.models if m.provider == p.name]
    key = p.api_key
    return {
        "name": p.name, "type": p.type, "base_url": p.base_url, "source": p.source,
        "enabled": p.enabled, "available": p.available, "local": p.local,
        "key": {"required": p.requires_key, "set": bool(key), "source": p.key_status,
                "hint": mask(key) if key else None, "env": p.api_key_env},
        "enabled_env": p.enabled_env,
        "headers": {name: "••••" for name in p.headers},
        "model_count": len(models), "enabled_models": sum(m.enabled for m in models),
    }


def model_view(request: Request, m: ModelSpec) -> dict[str, Any]:
    svc = _svc(request)
    return {**m.model_dump(), "source": "custom" if svc.is_custom_model(m.id) else "builtin",
            "available": request.app.state.registry.is_available(m), "overridden": svc.overridden(m.id)}


@router.get("")
async def settings_overview(request: Request):
    svc = _svc(request)
    return {"access": "admin_key" if request.app.state.settings.jev_router_admin_key else "loopback",
            "secret_source": svc.box.source, "warnings": svc.warnings}


@router.get("/providers")
async def get_providers(request: Request):
    registry = request.app.state.registry
    return {"data": [provider_view(request, p) for p in registry.providers.values()], "warnings": _svc(request).warnings}


@router.post("/providers", status_code=201)
async def create_provider(body: ProviderCreate, request: Request):
    data = body.model_dump()
    if data["local"] is None:
        data["local"] = is_local_url(data["base_url"])
    await _svc(request).add_provider(data)
    return provider_view(request, request.app.state.registry.providers[body.name])


@router.post("/providers/test")
async def probe_unsaved_provider(body: ProviderProbe, request: Request):
    return await probe(request.app.state.http, body.type, body.base_url, body.api_key, body.headers)


@router.patch("/providers/{name}")
async def patch_provider(name: str, body: ProviderPatch, request: Request):
    changes = body.model_dump(exclude_unset=True)
    _reject_nulls(changes, {"api_key", "headers"})
    await _svc(request).update_provider(name, changes)
    return provider_view(request, _provider_or_404(request, name))


@router.delete("/providers/{name}", status_code=204)
async def delete_provider(name: str, request: Request):
    await _svc(request).delete_provider(name)
    return Response(status_code=204)


@router.post("/providers/{name}/test")
async def probe_saved_provider(name: str, request: Request, body: SavedProviderProbe | None = None):
    p = _provider_or_404(request, name)
    body = body or SavedProviderProbe()
    return await probe(request.app.state.http, p.type, body.base_url or p.base_url, body.api_key or p.api_key, p.headers)


@router.get("/providers/{name}/discover")
async def discover(name: str, request: Request):
    p = _provider_or_404(request, name)
    try:
        names = await list_models(request.app.state.http, p.type, p.base_url, p.api_key, p.headers)
    except DiscoveryError as e:
        raise APIError(502, str(e), "discovery_failed", "upstream_error") from None
    registry = request.app.state.registry
    ids = {m.id for m in registry.models}
    upstream_names = {m.model for m in registry.models if m.provider == name}
    return {"data": [
        {"model": n, "id": f"{name}/{n}", "registered": n in upstream_names or f"{name}/{n}" in ids,
         "suggested": SettingsService.model_defaults(name, n)}
        for n in names
    ]}


@router.get("/models")
async def get_models(request: Request):
    registry = request.app.state.registry
    return {"data": [model_view(request, m) for m in registry.models], "warnings": _svc(request).warnings}


@router.post("/models", status_code=201)
async def create_models(body: list[ModelCreate], request: Request):
    if not 1 <= len(body) <= 200:
        raise APIError(400, "Send between 1 and 200 models.", "invalid_request")
    ids = await _svc(request).add_models([item.model_dump(exclude_none=True) for item in body])
    return {"data": [model_view(request, _model_or_404(request, mid)) for mid in ids]}


@router.patch("/models/{model_id:path}")
async def patch_model(model_id: str, body: ModelFields, request: Request):
    changes = body.model_dump(exclude_unset=True)
    _reject_nulls(changes, NULLABLE_MODEL_FIELDS)
    await _svc(request).update_model(model_id, changes)
    return model_view(request, _model_or_404(request, model_id))


@router.delete("/models/{model_id:path}", status_code=204)
async def delete_model(model_id: str, request: Request):
    await _svc(request).delete_model(model_id)
    return Response(status_code=204)


@router.get("/export")
async def export(request: Request):
    svc, registry = _svc(request), request.app.state.registry
    providers: dict[str, dict[str, Any]] = {}
    for name, override in sorted(svc.overlay.provider_overrides.items()):
        p = registry.providers.get(name)
        if p and override.base_url:
            providers[name] = p.model_dump(
                include={"type", "base_url", "api_key_env", "requires_key", "enabled_env", "local"}, exclude_defaults=True,
            ) | {"base_url": p.base_url}
    for name in sorted(svc.overlay.custom_providers):
        p = registry.providers.get(name)
        if p is None or p.source != "custom":
            continue
        entry: dict[str, Any] = {"type": p.type, "base_url": p.base_url}
        if p.stored_key or p.stored_key_unreadable:
            entry["api_key_env"] = f"{name.upper().replace('-', '_')}_API_KEY"
        else:
            entry["requires_key"] = False
        if p.local:
            entry["local"] = True
        providers[name] = entry
    wanted = set(svc.overlay.custom_models) | set(svc.overlay.model_overrides)
    models = [m.model_dump(exclude_defaults=True) for m in registry.models if m.id in wanted]
    text = ("# Jev Router settings export. Merge into config/models.yaml.\n"
            "# Keys and header values are not exported: set each api_key_env in .env.\n"
            + yaml.safe_dump({"providers": providers, "models": models}, sort_keys=False, allow_unicode=True))
    return PlainTextResponse(text, media_type="text/yaml")
