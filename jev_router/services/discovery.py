import json
import time

import httpx

MAX_BYTES = 1_000_000
MAX_MODELS = 500
TIMEOUT = 5.0
ANTHROPIC_VERSION = "2023-06-01"


class DiscoveryError(Exception):
    pass


def _scrub(text: str, secrets: list[str | None]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "****")
    return text[:300]


def _request_headers(type_: str, api_key: str | None, headers: dict[str, str] | None) -> dict[str, str]:
    out = dict(headers or {})
    if type_ == "anthropic":
        out["anthropic-version"] = ANTHROPIC_VERSION
        if api_key:
            out["x-api-key"] = api_key
    elif api_key:
        out["Authorization"] = f"Bearer {api_key}"
    return out


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    async with client.stream("GET", url, headers=headers, timeout=TIMEOUT, follow_redirects=False) as r:
        body = b""
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise DiscoveryError("Response is larger than 1 MB.")
        return r.status_code, body


async def list_models(
    client: httpx.AsyncClient,
    type_: str,
    base_url: str,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
) -> list[str]:
    secrets = [api_key, *(headers or {}).values()]
    request_headers = _request_headers(type_, api_key, headers)
    base = base_url.rstrip("/")
    try:
        status, body = await _get(client, f"{base}/models", request_headers)
        list_key, name_key = "data", "id"
        if status == 404 and type_ == "openai":
            origin = base[:-3] if base.endswith("/v1") else base
            status, body = await _get(client, f"{origin}/api/tags", request_headers)
            list_key, name_key = "models", "name"
        if status != 200:
            raise DiscoveryError(f"HTTP {status}: {body.decode(errors='replace')}")
        items = json.loads(body).get(list_key) or []
        names = [str(i[name_key]) for i in items if isinstance(i, dict) and i.get(name_key)]
    except DiscoveryError as e:
        raise DiscoveryError(_scrub(str(e), secrets)) from None
    except httpx.TimeoutException:
        raise DiscoveryError(f"Timed out after {TIMEOUT:g} s.") from None
    except httpx.HTTPError as e:
        raise DiscoveryError(_scrub(f"Connection failed: {e}", secrets)) from None
    except (ValueError, AttributeError, TypeError):
        raise DiscoveryError("Unexpected response format; expected an OpenAI-style model list.") from None
    return list(dict.fromkeys(names))[:MAX_MODELS]


async def probe(
    client: httpx.AsyncClient,
    type_: str,
    base_url: str,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    started = time.perf_counter()
    try:
        found = await list_models(client, type_, base_url, api_key, headers)
    except DiscoveryError as e:
        return {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "models_found": 0, "error": str(e)}
    return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "models_found": len(found), "error": None}
