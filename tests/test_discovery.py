import asyncio

import h11
import httpx
import pytest

from jev_router.services import discovery
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
    (lambda r: httpx.Response(401, text="x" * 282 + "sk-secret-123456" + "y" * 700), "HTTP 401"),
])
def test_failures_are_scrubbed_and_trimmed(response, fragment):
    with pytest.raises(DiscoveryError) as e:
        call(response, api_key="sk-secret-123456", headers={"X-Team": "core-secret"})
    message = str(e.value)
    assert fragment in message and len(message) <= 300
    assert "sk-secret-123456" not in message and "core-secret" not in message and "sk-secre" not in message


def test_probe_reports_instead_of_raising():
    ok = call(lambda r: httpx.Response(200, json={"data": [{"id": "a"}]}), fn=probe)
    assert ok["ok"] and ok["models_found"] == 1 and ok["error"] is None and ok["latency_ms"] >= 0
    bad = call(lambda r: httpx.Response(500, text="boom"), fn=probe)
    assert bad["ok"] is False and bad["models_found"] == 0 and "HTTP 500" in bad["error"]


def test_escaped_secret_in_protocol_error_is_scrubbed():
    def handler(request):
        try:
            h11.Request(method="GET", target="/", headers=list(request.headers.raw))
        except h11.LocalProtocolError as e:
            raise httpx.LocalProtocolError(str(e)) from None
        return httpx.Response(200, json={"data": []})
    with pytest.raises(DiscoveryError) as e:
        call(handler, api_key="sk-secret-123456\n")
    message = str(e.value)
    assert "Illegal header value" in message
    assert "sk-secret-123456" not in message and "sk-secret-123456\\n" not in message


def test_deeply_nested_json_is_a_format_error():
    def handler(request):
        return httpx.Response(200, content=b"[" * 200000)
    with pytest.raises(DiscoveryError, match="Unexpected response"):
        call(handler)
    result = call(handler, fn=probe)
    assert result["ok"] is False and "Unexpected response" in result["error"]


def test_total_deadline_covers_slow_body(monkeypatch):
    monkeypatch.setattr(discovery, "TIMEOUT", 0.05)

    async def drip():
        yield b'{"data": ['
        await asyncio.sleep(0.5)
        yield b"]}"

    with pytest.raises(DiscoveryError, match="Timed out"):
        call(lambda r: httpx.Response(200, content=drip()))


def test_non_ascii_header_value():
    with pytest.raises(DiscoveryError) as e:
        call(lambda r: httpx.Response(200, json={"data": []}), headers={"X-Team": "caf\u00e9"})
    assert str(e.value) == "Header values must be ASCII."


def test_unexpected_transport_error_is_a_generic_failure():
    def handler(request):
        raise OverflowError("connect(): port must be 0-65535.")
    with pytest.raises(DiscoveryError) as e:
        call(handler, api_key="sk-secret-123456")
    assert str(e.value) == "Connection failed."
    result = call(handler, fn=probe)
    assert result["ok"] is False and result["error"] == "Connection failed."


def test_redirect_is_not_followed():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(302, headers={"Location": "http://elsewhere/v1/models"})
    with pytest.raises(DiscoveryError, match="HTTP 302"):
        call(handler)
    assert seen == ["/v1/models"]
