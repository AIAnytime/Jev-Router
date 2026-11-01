import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..engine.features import content_text
from ..registry import ModelSpec, ProviderSpec
from .base import Provider, ProviderError, iter_sse, raise_for_status

API_VERSION = "2023-06-01"
STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header[5:].split(";")[0] or "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_blocks(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks = []
    for part in content:
        kind = part.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif kind == "image_url":
            image = part.get("image_url")
            blocks.append(_image_block(image.get("url", "") if isinstance(image, dict) else str(image)))
    return blocks


def to_anthropic(model: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
    system: list[str] = []
    messages: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for msg in payload.get("messages", []):
        role = msg.get("role")
        if role in ("system", "developer"):
            system.append(content_text(msg.get("content")))
        elif role == "tool":
            push("user", [{
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": content_text(msg.get("content")),
            }])
        elif role == "assistant":
            blocks = _content_blocks(msg.get("content"))
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                blocks.append({"type": "tool_use", "id": call.get("id", ""), "name": fn.get("name", ""), "input": args})
            push("assistant", blocks)
        else:
            push("user", _content_blocks(msg.get("content")))

    body: dict[str, Any] = {
        "model": model.model,
        "messages": messages,
        "max_tokens": payload.get("max_completion_tokens") or payload.get("max_tokens") or model.max_output_tokens or 4096,
    }
    if system:
        body["system"] = "\n\n".join(s for s in system if s)
    for key in ("temperature", "top_p"):
        if payload.get(key) is not None:
            body[key] = payload[key]
    stop = payload.get("stop")
    if stop:
        body["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if payload.get("tools"):
        body["tools"] = [
            {
                "name": (t.get("function") or {}).get("name"),
                "description": (t.get("function") or {}).get("description", ""),
                "input_schema": (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}},
            }
            for t in payload["tools"]
        ]
        choice = payload.get("tool_choice")
        if choice == "required":
            body["tool_choice"] = {"type": "any"}
        elif choice == "none":
            body["tool_choice"] = {"type": "none"}
        elif isinstance(choice, dict) and choice.get("function"):
            body["tool_choice"] = {"type": "tool", "name": choice["function"].get("name")}
    return body


def _usage(u: dict[str, Any]) -> dict[str, int]:
    prompt = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    out = u.get("output_tokens", 0)
    return {"prompt_tokens": prompt, "completion_tokens": out, "total_tokens": prompt + out}


def from_anthropic(data: dict[str, Any], model_id: str) -> dict[str, Any]:
    text, calls = [], []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            calls.append({
                "id": block.get("id"),
                "type": "function",
                "function": {"name": block.get("name"), "arguments": json.dumps(block.get("input", {}))},
            })
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": data.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_id,
        "choices": [{"index": 0, "message": message, "finish_reason": STOP_REASONS.get(data.get("stop_reason"), "stop")}],
        "usage": _usage(data.get("usage", {})),
    }


class AnthropicProvider(Provider):
    def _headers(self, spec: ProviderSpec) -> dict[str, str]:
        return {
            "x-api-key": spec.api_key or "",
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
            **spec.headers,
        }

    async def complete(self, spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.client.post(
                f"{spec.base_url.rstrip('/')}/messages", headers=self._headers(spec), json=to_anthropic(model, payload)
            )
        except httpx.HTTPError as e:
            raise ProviderError(503, f"anthropic unreachable: {e.__class__.__name__}") from e
        await raise_for_status(resp)
        return from_anthropic(resp.json(), model.model)

    async def stream(
        self, spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        body = {**to_anthropic(model, payload), "stream": True}
        chunk_id, created = f"chatcmpl-{uuid.uuid4().hex}", int(time.time())
        usage: dict[str, Any] = {}
        tool_index: dict[int, int] = {}

        def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
            return {
                "id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }

        try:
            async with self.client.stream(
                "POST", f"{spec.base_url.rstrip('/')}/messages", headers=self._headers(spec), json=body
            ) as resp:
                await raise_for_status(resp)
                async for event, data in iter_sse(resp):
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    kind = ev.get("type", event)
                    if kind == "message_start":
                        usage.update(ev.get("message", {}).get("usage", {}))
                        yield chunk({"role": "assistant", "content": ""})
                    elif kind == "content_block_start":
                        block = ev.get("content_block", {})
                        if block.get("type") == "tool_use":
                            idx = tool_index.setdefault(ev.get("index", 0), len(tool_index))
                            yield chunk({"tool_calls": [{
                                "index": idx, "id": block.get("id"), "type": "function",
                                "function": {"name": block.get("name"), "arguments": ""},
                            }]})
                    elif kind == "content_block_delta":
                        delta = ev.get("delta", {})
                        if delta.get("type") == "text_delta":
                            yield chunk({"content": delta.get("text", "")})
                        elif delta.get("type") == "input_json_delta":
                            idx = tool_index.get(ev.get("index", 0), 0)
                            yield chunk({"tool_calls": [{"index": idx, "function": {"arguments": delta.get("partial_json", "")}}]})
                    elif kind == "message_delta":
                        usage.update(ev.get("usage", {}))
                        reason = ev.get("delta", {}).get("stop_reason")
                        yield chunk({}, STOP_REASONS.get(reason, "stop"))
                    elif kind == "error":
                        raise ProviderError(502, ev.get("error", {}).get("message", "anthropic stream error"))
        except httpx.HTTPError as e:
            raise ProviderError(503, f"anthropic stream failed: {e.__class__.__name__}") from e
        yield {
            "id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model.model,
            "choices": [], "usage": _usage(usage),
        }
