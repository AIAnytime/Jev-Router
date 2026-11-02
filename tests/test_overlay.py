from pathlib import Path

import pytest

from jev_router.crypto import SecretBox
from jev_router.overlay import CustomProvider, Overlay, ProviderOverride, merge
from jev_router.registry import Registry

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def base(provider_env):
    reg = Registry.load(ROOT / "config")
    return reg.base_providers, reg.base_models


@pytest.fixture
def box(tmp_path):
    return SecretBox.from_settings("test-secret", tmp_path / "k")


def run(base, box, overlay):
    return merge(base[0], base[1], overlay, box)


def test_empty_overlay_is_a_copy_of_base(base, box):
    r = run(base, box, Overlay())
    assert r.warnings == [] and r.errors == []
    assert set(r.providers) == set(base[0])
    assert [m.id for m in r.models] == [m.id for m in base[1]]
    assert r.providers["openai"] is not base[0]["openai"]
    assert r.models[0] is not base[1][0]


def test_settings_key_beats_env_beats_none(base, box, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    saved = Overlay(provider_overrides={"openai": ProviderOverride(api_key_enc=box.encrypt("saved-openai-key"))})
    openai = run(base, box, saved).providers["openai"]
    assert (openai.api_key, openai.key_status) == ("saved-openai-key", "settings")
    plain = run(base, box, Overlay()).providers
    assert (plain["openai"].api_key, plain["openai"].key_status) == ("env-openai-key", "env")
    assert (plain["groq"].api_key, plain["groq"].key_status) == (None, None)


def test_unreadable_key_falls_back_to_env(base, box, tmp_path, monkeypatch):
    other = SecretBox.from_settings("other-secret", tmp_path / "o")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai-key")
    r = run(base, box, Overlay(provider_overrides={"openai": ProviderOverride(api_key_enc=other.encrypt("lost"))}))
    openai = r.providers["openai"]
    assert openai.key_status == "unreadable" and openai.api_key == "env-openai-key" and openai.available
    assert any("could not be decrypted" in w for w in r.warnings)


def test_enabled_override_and_base_url(base, box):
    assert not run(base, box, Overlay()).providers["ollama"].available
    on = run(base, box, Overlay(provider_overrides={
        "ollama": ProviderOverride(enabled=True, base_url="http://10.0.0.5:11434/v1"),
        "liquid": ProviderOverride(enabled=False),
    })).providers
    assert on["ollama"].available and on["ollama"].base_url == "http://10.0.0.5:11434/v1"
    assert not on["liquid"].available


def test_model_overrides_custom_provider_and_models(base, box):
    overlay = Overlay(
        model_overrides={"liquid/lfm3-1.7b": {"speed": 3.0, "enabled": False}},
        custom_providers={"lab": CustomProvider(
            name="lab", base_url="http://localhost:1234/v1", local=True,
            api_key_enc=box.encrypt("lab-key"), headers_enc=box.encrypt('{"X-Team": "core"}'))},
        custom_models={"lab/qwen3": {"provider": "lab", "model": "qwen3", "description": "Qwen 3 on lab"}},
    )
    r = run(base, box, overlay)
    assert r.warnings == [] and r.errors == []
    small = next(m for m in r.models if m.id == "liquid/lfm3-1.7b")
    assert small.speed == 3.0 and small.enabled is False
    assert next(m for m in base[1] if m.id == "liquid/lfm3-1.7b").speed == 9
    lab = r.providers["lab"]
    assert lab.source == "custom" and lab.api_key == "lab-key" and lab.headers == {"X-Team": "core"}
    assert lab.available
    qwen = next(m for m in r.models if m.id == "lab/qwen3")
    assert qwen.local is True and qwen.quality == 5.0


def test_merge_never_mutates_base(base, box):
    providers, models = base
    before_providers = {n: (p.model_dump(), p.stored_key) for n, p in providers.items()}
    before_models = [m.model_dump() for m in models]
    overlay = Overlay(
        provider_overrides={
            "openai": ProviderOverride(api_key_enc=box.encrypt("saved-openai-key"), base_url="http://proxy/v1"),
            "ollama": ProviderOverride(enabled=True, base_url="http://10.0.0.5:11434/v1"),
        },
        custom_providers={"lab": CustomProvider(name="lab", base_url="http://localhost:1234/v1", local=True)},
        model_overrides={"liquid/lfm3-1.7b": {"speed": 3.0, "enabled": False}},
        custom_models={"lab/qwen3": {"provider": "lab", "model": "qwen3", "description": "Qwen 3 on lab"}},
    )
    r = run(base, box, overlay)
    assert r.providers["openai"].stored_key == "saved-openai-key"
    assert {n: (p.model_dump(), p.stored_key) for n, p in providers.items()} == before_providers
    assert [m.model_dump() for m in models] == before_models
    assert "lab" not in providers and "lab/qwen3" not in {m.id for m in models}
    assert all(a is not b for a, b in zip(r.models, models))


def test_problems_become_warnings_and_errors(base, box, tmp_path):
    other = SecretBox.from_settings("other-secret", tmp_path / "o")
    overlay = Overlay(
        provider_overrides={"groq": ProviderOverride(api_key_enc=other.encrypt("lost"))},
        custom_providers={"openai": CustomProvider(name="openai", base_url="http://x/v1")},
        model_overrides={"gone/model": {"speed": 2.0}},
        custom_models={
            "nope/m": {"provider": "nope", "model": "m", "description": "d"},
            "liquid/bad": {"provider": "liquid", "model": "bad", "description": "d", "quality": "high"},
        },
    )
    r = run(base, box, overlay)
    assert len(r.warnings) == 4, r.warnings
    assert len(r.errors) == 1 and "liquid/bad" in r.errors[0]
    assert not {"nope/m", "liquid/bad", "gone/model"} & {m.id for m in r.models}
    assert r.providers["openai"].source == "builtin"
