import asyncio

import httpx
import pytest

from jev_router.services.discovery import MAX_BYTES, DiscoveryError, list_models, probe


def call(handler, fn=list_models, type_="openai", base_url="http://localhost:1234/v1", **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fn(client, type_, base_url, **kw)
    return asyncio.run(go())


def test_openai_shape_with_auth_and_dedupe():
    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer k-123" and request.headers["x-team"] == "core"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"nope": 1}]})
    assert call(handler, api_key="k-123", headers={"X-Team": "core"}) == ["a", "b"]


def test_ollama_tags_fallback_after_404():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.2:3b"}, {"name": "qwen3:8b"}]})
        return httpx.Response(404, text="not found")
    assert call(handler, base_url="http://localhost:11434/v1/") == ["llama3.2:3b", "qwen3:8b"]
    assert seen == ["/v1/models", "/api/tags"]


def test_anthropic_headers():
    def handler(request):
        assert request.headers["x-api-key"] == "ak-1" and request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "claude-x"}]})
    assert call(handler, type_="anthropic", base_url="https://api.anthropic.com/v1", api_key="ak-1") == ["claude-x"]


def test_caps_models_at_500():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": f"m{i}"} for i in range(600)]})
    assert len(call(handler)) == 500


@pytest.mark.parametrize("response,fragment", [
    (lambda r: httpx.Response(401, text="invalid key sk-secret-123456 for X-Team core-secret"), "HTTP 401"),
    (lambda r: httpx.Response(200, content=b"x" * (MAX_BYTES + 10)), "larger than 1 MB"),
    (lambda r: httpx.Response(200, text="<html>"), "Unexpected response"),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "Timed out"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)), "Connection failed"),
])
def test_failures_are_scrubbed_and_trimmed(response, fragment):
    with pytest.raises(DiscoveryError) as e:
        call(response, api_key="sk-secret-123456", headers={"X-Team": "core-secret"})
    message = str(e.value)
    assert fragment in message and len(message) <= 300
    assert "sk-secret-123456" not in message and "core-secret" not in message


def test_probe_reports_instead_of_raising():
    ok = call(lambda r: httpx.Response(200, json={"data": [{"id": "a"}]}), fn=probe)
    assert ok["ok"] and ok["models_found"] == 1 and ok["error"] is None and ok["latency_ms"] >= 0
    bad = call(lambda r: httpx.Response(500, text="boom"), fn=probe)
    assert bad["ok"] is False and bad["models_found"] == 0 and "HTTP 500" in bad["error"]
