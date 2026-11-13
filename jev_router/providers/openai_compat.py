import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..registry import ModelSpec, ProviderSpec
from .base import Provider, ProviderError, iter_sse, raise_for_status


def _prompt_cache_key(spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]) -> str | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None

    initial: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role in {"system", "developer"}:
            initial.append({"role": role, "content": message.get("content")})
        elif role == "user":
            initial.append({"role": role, "content": message.get("content")})
            break
    if not any(message["role"] == "user" for message in initial):
        return None

    material = json.dumps(
        {
            "version": 1,
            "provider": spec.name,
            "model": model.model,
            "messages": initial,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class OpenAICompatProvider(Provider):
    """OpenAI, Liquid, Gemini, DeepSeek, Groq, OpenRouter, Ollama, vLLM and any /chat/completions API."""

    def _request(self, spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]) -> tuple[str, dict, dict]:
        headers = {"Content-Type": "application/json", **spec.headers}
        if spec.api_key:
            headers["Authorization"] = f"Bearer {spec.api_key}"
        body = {**payload, "model": model.model}
        # Some compatible gateways only derive cache identity from the request body.
        if (
            spec.supports_prompt_cache_key
            and "prompt_cache_key" not in body
            and "session_id" not in body
        ):
            key = _prompt_cache_key(spec, model, payload)
            if key:
                body["prompt_cache_key"] = key
        return f"{spec.base_url.rstrip('/')}/chat/completions", headers, body

    async def complete(self, spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        url, headers, body = self._request(spec, model, {**payload, "stream": False})
        body.pop("stream_options", None)
        try:
            resp = await self.client.post(url, headers=headers, json=body)
        except (httpx.HTTPError, httpx.InvalidURL, OSError, OverflowError, ExceptionGroup) as e:
            raise ProviderError(503, f"{spec.name} unreachable: {e.__class__.__name__}") from e
        await raise_for_status(resp)
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(502, f"{spec.name} returned invalid JSON") from e

    async def stream(
        self, spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        url, headers, body = self._request(spec, model, payload)
        body["stream"] = True
        options = body.get("stream_options")
        body["stream_options"] = {**(options if isinstance(options, dict) else {}), "include_usage": True}
        try:
            async with self.client.stream("POST", url, headers=headers, json=body) as resp:
                await raise_for_status(resp)
                async for _, data in iter_sse(resp):
                    if data == "[DONE]":
                        return
                    try:
                        yield json.loads(data)
                    except ValueError:
                        continue
        except (httpx.HTTPError, httpx.InvalidURL, OSError, OverflowError, ExceptionGroup) as e:
            raise ProviderError(503, f"{spec.name} stream failed: {e.__class__.__name__}") from e
