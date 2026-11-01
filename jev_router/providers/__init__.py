import httpx

from .anthropic import AnthropicProvider
from .base import Provider, ProviderError
from .openai_compat import OpenAICompatProvider


def build_providers(client: httpx.AsyncClient) -> dict[str, Provider]:
    return {"openai": OpenAICompatProvider(client), "anthropic": AnthropicProvider(client)}


__all__ = ["Provider", "ProviderError", "build_providers"]
