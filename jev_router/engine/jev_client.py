import asyncio
from typing import Any

import httpx


class JevError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class JevClient:
    """Thin async client for Jev's `POST /v1/systemone` typed-decision endpoint."""

    MAX_QUESTIONS = 10
    RETRY_STATUSES = {429, 500, 502, 503, 504}

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.typesafe.ai/v1",
        model: str = "jev-latest",
        timeout: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if not self.configured:
            raise JevError("JEV_API_KEY is not set")
        if not 0 < len(questions) <= self.MAX_QUESTIONS:
            raise JevError(f"Jev accepts 1-{self.MAX_QUESTIONS} questions, got {len(questions)}")
        body = {"model": self.model, "state": state, "questions": questions}

        last_error: JevError | None = None
        for attempt in range(2):
            try:
                resp = await self._client.post("/systemone", json=body)
            except httpx.HTTPError as e:
                last_error = JevError(f"Jev request failed: {e.__class__.__name__}")
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data.get("answers"), dict):
                        raise JevError("Jev response has no answers")
                    return data
                last_error = JevError(f"Jev returned {resp.status_code}: {resp.text[:300]}", resp.status_code)
                if resp.status_code not in self.RETRY_STATUSES:
                    break
            if attempt == 0:
                await asyncio.sleep(0.25)
        raise last_error  # type: ignore[misc]

    async def aclose(self) -> None:
        await self._client.aclose()
