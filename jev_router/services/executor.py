import time
from dataclasses import asdict, dataclass
from typing import Any

from ..providers import Provider, ProviderError
from ..registry import ModelSpec, ProviderSpec, Registry


@dataclass
class Attempt:
    model: str
    provider: str
    status: str
    latency_ms: float
    http_status: int | None = None
    error: str | None = None


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[Attempt]):
        self.attempts = attempts
        last = attempts[-1] if attempts else None
        self.status = (last.http_status if last else None) or 502
        super().__init__(f"all {len(attempts)} provider attempt(s) failed; last: {last.error if last else 'n/a'}")


def client_status(upstream: int | None) -> int:
    """Map the last upstream status to the status the router returns to its own client.

    Upstream 401/403/404 mean the router's provider credentials or model ids are wrong, not the caller's,
    so they become 502 instead of making the OpenAI SDK report a bad router key. Request-shape errors
    (400/413/422), rate limits (429) and gateway errors (502/503/504) keep their meaning and pass through.
    """
    if upstream in (400, 413, 422, 429, 502, 503, 504):
        return upstream
    return 502


def attempts_dict(attempts: list[Attempt]) -> list[dict[str, Any]]:
    return [asdict(a) for a in attempts]


class Executor:
    """Runs the ranked candidates in order, falling back to the next model on any provider error."""

    def __init__(self, registry: Registry, providers: dict[str, Provider]):
        self.registry = registry
        self.providers = providers

    def _provider(self, model: ModelSpec, specs: dict[str, ProviderSpec] | None = None) -> tuple[Provider, ProviderSpec]:
        spec = specs[model.provider] if specs is not None else self.registry.provider_for(model)
        return self.providers[spec.type], spec

    async def complete(
        self, chain: list[ModelSpec], payload: dict[str, Any], specs: dict[str, ProviderSpec] | None = None
    ) -> tuple[ModelSpec, dict[str, Any], list[Attempt]]:
        """`specs` is the provider snapshot taken at planning time; without it the live registry is used."""
        attempts: list[Attempt] = []
        for model in chain:
            provider, spec = self._provider(model, specs)
            start = time.perf_counter()
            try:
                result = await provider.complete(spec, model, payload)
            except ProviderError as e:
                attempts.append(Attempt(model.id, spec.name, "error", _ms(start), e.status, e.message))
                continue
            attempts.append(Attempt(model.id, spec.name, "success", _ms(start), 200))
            return model, result, attempts
        raise AllProvidersFailed(attempts)

    async def open_stream(self, chain: list[ModelSpec], payload: dict[str, Any], specs: dict[str, ProviderSpec] | None = None):
        """Return (model, first_chunk, iterator, attempts). Fallback is only possible before the first chunk."""
        attempts: list[Attempt] = []
        for model in chain:
            provider, spec = self._provider(model, specs)
            start = time.perf_counter()
            stream = provider.stream(spec, model, payload)
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                attempts.append(Attempt(model.id, spec.name, "error", _ms(start), 502, "empty stream"))
                continue
            except ProviderError as e:
                attempts.append(Attempt(model.id, spec.name, "error", _ms(start), e.status, e.message))
                await stream.aclose()
                continue
            except BaseException:
                await stream.aclose()
                raise
            attempts.append(Attempt(model.id, spec.name, "success", _ms(start), 200))
            return model, first, stream, attempts
        raise AllProvidersFailed(attempts)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)
