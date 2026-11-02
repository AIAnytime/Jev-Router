import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jev_router.config import Settings
from jev_router.main import create_app

ROOT = Path(__file__).resolve().parent.parent
PROVIDER_ENVS = ["LIQUID_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY",
                 "GROQ_API_KEY", "OPENROUTER_API_KEY", "OLLAMA_ENABLED"]


class FakeUpstream:
    """Mock for Jev and every provider, served through an httpx.MockTransport."""

    def __init__(self):
        self.jev_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_urls: list[str] = []
        self.anthropic_calls: list[dict[str, Any]] = []
        self.route_choice: str | None = None
        self.route_confidence = 0.9
        self.complexity = 0.3
        self.premium = 0.05
        self.task = "chat"
        self.guard: dict[str, float] = {}
        self.choices: dict[str, str] = {}
        self.jev_fail = False
        self.fail_models: set[str] = set()
        self.reply = "Hello from the mock model."

    # -- Jev ---------------------------------------------------------------
    def _jev(self, body: dict[str, Any]) -> httpx.Response:
        self.jev_calls.append(body)
        if self.jev_fail:
            return httpx.Response(500, json={"error": "boom"})
        answers: dict[str, Any] = {}
        for name, q in body["questions"].items():
            if q["type"] == "choice":
                options = list(q["criteria"])
                pick = self.choices.get(name)
                if name == "model_route":
                    pick = self.route_choice
                elif name == "task_type":
                    pick = self.task
                pick = pick if pick in options else options[0]
                rest = (1 - self.route_confidence) / max(1, len(options) - 1)
                probs = {o: (self.route_confidence if o == pick else rest) for o in options}
                answers[name] = {"choice": pick, "confidence": self.route_confidence, "probabilities": probs}
            elif q["type"] == "score":
                idx = round(self.complexity)
                probs = {str(i): (0.85 if i == idx else 0.05) for i in range(4)}
                answers[name] = {"score": self.complexity, "confidence": 0.85, "probabilities": probs,
                                 "legend": {str(i): c for i, c in enumerate(q["criteria"])}}
            else:
                p = self.premium if name == "premium_required" else self.guard.get(name, 0.02)
                answers[name] = {"noul": p}
        return httpx.Response(200, json={"answers": answers, "model": "jev-test", "usage": {"total_tokens": 10}})

    # -- OpenAI-compatible providers ----------------------------------------
    def _chat(self, body: dict[str, Any]) -> httpx.Response:
        self.chat_calls.append(body)
        model = body["model"]
        if model in self.fail_models:
            return httpx.Response(503, json={"error": {"message": f"{model} overloaded"}})
        usage = {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30, "cost": 0.000004}
        if body.get("stream"):
            chunks = [
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
                *[{"id": "c1", "object": "chat.completion.chunk", "model": model,
                   "choices": [{"index": 0, "delta": {"content": w}, "finish_reason": None}]} for w in ["Hello ", "stream"]],
                {"id": "c1", "object": "chat.completion.chunk", "model": model,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"id": "c1", "object": "chat.completion.chunk", "model": model, "choices": [], "usage": usage},
            ]
            text = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(200, text=text, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": self.reply}, "finish_reason": "stop"}],
            "usage": usage,
        })

    def _anthropic(self, body: dict[str, Any]) -> httpx.Response:
        self.anthropic_calls.append(body)
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": "Hi from Claude."}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 4},
        })

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        if request.url.host == "api.typesafe.ai":
            return self._jev(body)
        if request.url.path.endswith("/chat/completions"):
            self.chat_urls.append(str(request.url))
            return self._chat(body)
        if request.url.path.endswith("/messages"):
            return self._anthropic(body)
        return httpx.Response(404, json={"error": "unexpected url " + str(request.url)})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def upstream() -> FakeUpstream:
    return FakeUpstream()


@pytest.fixture
def provider_env(monkeypatch):
    for name in PROVIDER_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LIQUID_API_KEY", "test-liquid")


@pytest.fixture
def make_client(tmp_path, upstream, provider_env):
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        values: dict[str, Any] = {
            "jev_api_key": "test-jev",
            "jev_router_api_keys": "",
            "jev_router_config_dir": str(ROOT / "config"),
            "database_url": f"sqlite+aiosqlite:///{tmp_path}/traces-{len(clients)}.db",
            "rate_limit_rpm": 0,
            "decision_cache_ttl": 0,
            "default_strategy": "balanced",
            "jev_router_admin_key": "",
            "jev_router_secret": "test-secret",
            "jev_router_secret_file": str(tmp_path / "secret.key"),
        }
        values.update(overrides)
        client = TestClient(create_app(Settings(**values), transport=upstream.transport))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client) -> TestClient:
    return make_client()
