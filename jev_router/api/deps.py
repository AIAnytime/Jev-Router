import hashlib
import hmac
import time
from collections import defaultdict
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str, type_: str = "invalid_request_error", **extra: Any):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.type = type_
        self.extra = extra


def error_response(status: int, message: str, code: str, type_: str = "invalid_request_error", **extra: Any) -> JSONResponse:
    error = {"message": message, "type": type_, "code": code, **{k: v for k, v in extra.items() if v is not None}}
    return JSONResponse({"error": error}, status_code=status)


class RateLimiter:
    """Fixed-window per-key limiter (requests per minute). In-memory, per process."""

    def __init__(self, rpm: int):
        self.rpm = rpm
        self._windows: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))

    def check(self, key: str) -> tuple[bool, int]:
        if self.rpm <= 0:
            return True, 0
        window = int(time.time() // 60)
        start, count = self._windows[key]
        if start != window:
            start, count = window, 0
        if count >= self.rpm:
            return False, 60 - int(time.time() % 60)
        self._windows[key] = (start, count + 1)
        return True, 0


def _extract_key(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key")


async def authenticate(request: Request) -> str:
    """Validate the router API key (if configured) and enforce the rate limit. Returns a client id."""
    settings = request.app.state.settings
    keys: set[str] = settings.api_keys
    supplied = _extract_key(request)
    if keys:
        if not supplied or not any(hmac.compare_digest(supplied, k) for k in keys):
            raise APIError(401, "Invalid or missing Jev Router API key.", "invalid_api_key", "authentication_error")
        client = hashlib.sha256(supplied.encode()).hexdigest()[:16]
    else:
        client = request.client.host if request.client else "anonymous"
    ok, retry = request.app.state.limiter.check(client)
    if not ok:
        raise APIError(429, f"Rate limit of {settings.rate_limit_rpm} requests/minute exceeded.", "rate_limit_exceeded",
                       "rate_limit_error", retry_after=retry)
    return client
