import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from ..registry import ModelSpec, ProviderSpec


class ProviderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


async def raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    body = (await resp.aread()).decode(errors="replace")
    try:
        err = json.loads(body).get("error")
        msg = err.get("message") if isinstance(err, dict) else (err or body)
    except (ValueError, AttributeError):
        msg = body
    raise ProviderError(resp.status_code, str(msg)[:500] or f"HTTP {resp.status_code}")


async def iter_sse(resp: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event, data) pairs from a server-sent-events response."""
    event: str | None = None
    async for line in resp.aiter_lines():
        if not line:
            event = None
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            yield event, line[5:].strip()


class Provider:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def complete(self, spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def stream(self, spec: ProviderSpec, model: ModelSpec, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        raise NotImplementedError
