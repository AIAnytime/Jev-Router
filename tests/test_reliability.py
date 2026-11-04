"""Regression tests: a misbehaving dependency must degrade a request, never turn it into a bare 500."""
import asyncio

import httpx
import pytest

from jev_router.providers import ProviderError
from jev_router.providers.anthropic import AnthropicProvider
from jev_router.registry import ModelSpec, ProviderSpec
from jev_router.schemas import ChatCompletionRequest
from jev_router.services.executor import client_status

SMALL = "liquid/lfm3-1.7b"
LARGE = "liquid/lfm2.5-24b-a2b"
MESSAGES = [{"role": "user", "content": "hey, how are you?"}]


def chat(client, **body):
    return client.post("/v1/chat/completions", json={"model": "auto", "messages": MESSAGES, **body})


# -- streaming -------------------------------------------------------------

@pytest.mark.parametrize("options", [None, "yes", 1])
def test_stream_accepts_null_or_odd_stream_options(client, options):
    r = chat(client, stream=True, stream_options=options)
    assert r.status_code == 200
    assert r.text.rstrip().endswith("data: [DONE]")


def test_stream_closes_upstream_when_client_stops_reading(client):
    router = client.app.state.model_router
    model = client.app.state.registry.get_model(SMALL)
    closed = []

    async def upstream():
        try:
            while True:
                yield {"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]}
        finally:
            closed.append(True)

    async def open_stream(chain, payload, providers):
        gen = upstream()
        return model, await anext(gen), gen, []

    router.executor.open_stream = open_stream

    async def run():
        body = ChatCompletionRequest.model_validate({"model": "auto", "messages": MESSAGES, "stream": True})
        events, _ = await router.chat_stream(body)
        await anext(events)
        await events.aclose()  # what Starlette does when the client disconnects

    client.portal.call(run)
    assert closed == [True]


# -- Jev failures fall back to the heuristic router ------------------------

@pytest.mark.parametrize("response", [
    httpx.Response(200, text="<html>bad gateway</html>"),
    httpx.Response(200, json=["not", "an", "object"]),
    httpx.Response(200, json={"answers": {"model_route": {"choice": SMALL, "confidence": None}}}),
    httpx.Response(200, json={"answers": {"complexity": {"probabilities": {"hard": 0.9}}}}),
])
def test_malformed_jev_response_uses_heuristic_router(client, upstream, response):
    upstream.jev_raw = response
    r = chat(client)
    assert r.status_code == 200
    trace = r.json()["jev_router"]
    assert trace["decision_engine"] == "heuristic_fallback"
    assert trace["error"]


# -- trace storage must not cost the client its response -------------------

def test_trace_store_failure_keeps_successful_response(client, upstream, caplog):
    async def broken(trace):
        raise RuntimeError("database is locked")

    client.app.state.store.record = broken
    r = chat(client)
    assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == upstream.reply
    assert client.post("/v1/route/model", json={"prompt": "dry"}).status_code == 200
    assert "failed to record trace" in caplog.text


def test_trace_store_failure_keeps_upstream_error(client, upstream):
    async def broken(trace):
        raise RuntimeError("database is locked")

    client.app.state.store.record = broken
    upstream.fail_models = {"lfm3-1.7b", "lfm2.5-24b-a2b"}
    r = chat(client)
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream_error"


# -- upstream status codes -------------------------------------------------

@pytest.mark.parametrize("upstream_status,expected", [
    (400, 400), (413, 413), (422, 422), (429, 429),
    (401, 502), (403, 502), (404, 502), (500, 502),
    (502, 502), (503, 503), (504, 504), (None, 502),
])
def test_client_status(upstream_status, expected):
    assert client_status(upstream_status) == expected


def test_provider_auth_error_is_not_reported_as_router_auth_error(client, upstream):
    upstream.fail_models, upstream.fail_status = {"lfm3-1.7b", "lfm2.5-24b-a2b"}, 401
    r = chat(client)
    assert r.status_code == 502
    err = r.json()["error"]
    assert err["code"] == "upstream_error"
    assert {a["http_status"] for a in err["details"]["attempts"]} == {401}


# -- providers -------------------------------------------------------------

@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=["a", "list"]),
])
def test_anthropic_invalid_body_is_a_provider_error(response):
    spec = ProviderSpec(name="anthropic", type="anthropic", base_url="https://api.anthropic.test/v1", api_key="k")
    model = ModelSpec(id="anthropic/claude", provider="anthropic", model="claude", description="c")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as http:
            await AnthropicProvider(http).complete(spec, model, {"messages": MESSAGES})

    with pytest.raises(ProviderError) as e:
        asyncio.run(run())
    assert e.value.status == 502


# -- unexpected errors keep the OpenAI error shape -------------------------

def test_unhandled_error_returns_openai_error_json(make_client):
    client = make_client(raise_server_exceptions=False)

    async def boom(*args, **kwargs):
        raise RuntimeError("unexpected")

    client.app.state.model_router.chat = boom
    r = chat(client)
    assert r.status_code == 500
    assert r.json()["error"] == {"message": "Internal error in Jev Router.", "type": "server_error", "code": "internal_error"}
