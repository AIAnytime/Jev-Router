import re

import httpx
import pytest

from jev_router.providers.openai_compat import OpenAICompatProvider
from jev_router.registry import ModelSpec, ProviderSpec


@pytest.fixture(scope="module")
def provider():
    return OpenAICompatProvider(httpx.AsyncClient())


def request(
    provider,
    payload,
    *,
    provider_name="test-provider",
    upstream_model="test-model",
    supports_prompt_cache_key=True,
):
    spec = ProviderSpec(
        name=provider_name,
        base_url="https://gateway.example/v1",
        requires_key=False,
        supports_prompt_cache_key=supports_prompt_cache_key,
    )
    model = ModelSpec(
        id=f"{provider_name}/{upstream_model}",
        provider=provider_name,
        model=upstream_model,
        description="test",
    )
    return provider._request(spec, model, payload)[2]


def conversation(*, system="stable system prompt", user="initial user request"):
    return {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
    }


def test_appended_turns_keep_the_same_cache_key(provider):
    first = request(provider, conversation())
    extended = conversation()
    extended["messages"].extend(
        [
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow-up question"},
            {"role": "developer", "content": "new turn-specific instruction"},
            {"role": "tool", "tool_call_id": "call-1", "content": "tool result"},
        ]
    )

    second = request(provider, extended)

    assert re.fullmatch(r"[0-9a-f]{64}", first["prompt_cache_key"])
    assert second["prompt_cache_key"] == first["prompt_cache_key"]


def test_different_initial_conversations_get_different_cache_keys(provider):
    first = request(provider, conversation(user="first conversation"))
    second = request(provider, conversation(user="second conversation"))

    assert first["prompt_cache_key"] != second["prompt_cache_key"]


def test_different_models_get_different_cache_keys(provider):
    first = request(provider, conversation(), upstream_model="model-a")
    second = request(provider, conversation(), upstream_model="model-b")

    assert first["prompt_cache_key"] != second["prompt_cache_key"]


def test_explicit_caller_cache_identity_is_unchanged(provider):
    prompt_key = request(provider, {**conversation(), "prompt_cache_key": "caller-key"})
    session_id = request(provider, {**conversation(), "session_id": "caller-session"})

    assert prompt_key["prompt_cache_key"] == "caller-key"
    assert "session_id" not in prompt_key
    assert session_id["session_id"] == "caller-session"
    assert "prompt_cache_key" not in session_id


def test_all_compatible_providers_get_a_key_by_default(provider):
    first = request(provider, conversation(), provider_name="provider-a")
    second = request(provider, conversation(), provider_name="provider-b")

    assert first["prompt_cache_key"]
    assert second["prompt_cache_key"]
    assert first["prompt_cache_key"] != second["prompt_cache_key"]


def test_provider_can_opt_out(provider):
    body = request(provider, conversation(), supports_prompt_cache_key=False)

    assert "prompt_cache_key" not in body


def test_payload_without_a_user_message_is_unchanged(provider):
    body = request(provider, {"messages": [{"role": "system", "content": "system only"}]})

    assert "prompt_cache_key" not in body


def test_caller_payload_is_not_mutated(provider):
    payload = conversation()

    body = request(provider, payload)

    assert "prompt_cache_key" not in payload
    assert body["prompt_cache_key"]


def test_stream_options_do_not_change_the_cache_key(provider):
    complete = request(provider, {**conversation(), "stream": False})
    stream = request(
        provider,
        {
            **conversation(),
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )

    assert stream["prompt_cache_key"] == complete["prompt_cache_key"]
