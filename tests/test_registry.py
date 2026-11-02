from pathlib import Path

import pytest

from jev_router.engine.cache import TTLCache
from jev_router.registry import ModelSpec, ProviderSpec, Registry

ROOT = Path(__file__).resolve().parent.parent


def spec(**kw):
    return ProviderSpec(name="p", base_url="http://p/v1", **kw)


def test_key_precedence(provider_env, monkeypatch):
    monkeypatch.setenv("P_KEY", "from-env")
    p = spec(api_key_env="P_KEY")
    assert (p.api_key, p.key_status) == ("from-env", "env")
    p.stored_key = "from-settings"
    assert (p.api_key, p.key_status) == ("from-settings", "settings")
    p.stored_key, p.stored_key_unreadable = None, True
    assert (p.api_key, p.key_status) == ("from-env", "unreadable")
    monkeypatch.delenv("P_KEY")
    assert (p.api_key, p.key_status) == (None, "unreadable")
    assert spec().key_status is None


def test_stored_key_is_never_dumped_or_printed():
    p = spec(stored_key="sk-secret-value", headers={"X-A": "header-secret"})
    assert "sk-secret-value" not in repr(p) and "header-secret" not in repr(p)
    assert "stored_key" not in p.model_dump()


def test_enabled_override(provider_env, monkeypatch):
    ollama = spec(requires_key=False, enabled_env="OLLAMA_ENABLED")
    assert not ollama.available
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    assert ollama.available
    ollama.enabled_override = False
    assert not ollama.available and not ollama.enabled
    monkeypatch.delenv("OLLAMA_ENABLED")
    ollama.enabled_override = True
    assert ollama.available
    keyed = spec(api_key_env="NOPE_KEY", enabled_override=True)
    assert keyed.enabled and not keyed.available


def test_validate_rejects_unknown_provider_and_duplicates():
    providers = {"p": spec()}
    m = ModelSpec(id="p/a", provider="p", model="a", description="a")
    Registry.validate(providers, [m])
    with pytest.raises(ValueError, match="unknown providers"):
        Registry.validate({}, [m])
    with pytest.raises(ValueError, match="duplicate model ids"):
        Registry.validate(providers, [m, m.model_copy()])


def test_base_is_a_copy_and_apply_swaps_in_place(provider_env):
    reg = Registry.load(ROOT / "config")
    assert reg.providers["ollama"].local is True
    reg.providers["openai"].base_url = "http://changed/v1"
    assert reg.base_providers["openai"].base_url == "https://api.openai.com/v1"
    lab = ProviderSpec(name="lab", base_url="http://lab/v1", requires_key=False, source="custom")
    model = ModelSpec(id="lab/m", provider="lab", model="m", description="m")
    reg.apply({**reg.base_providers, "lab": lab}, [*reg.base_models, model])
    assert reg.get_model("lab/m") is model and reg.is_available(model)
    with pytest.raises(ValueError):
        reg.apply(reg.base_providers, [model])
    assert reg.get_model("lab/m") is model


def test_cache_clear():
    cache = TTLCache(4, 60)
    cache.set("k", 1)
    cache.clear()
    assert cache.get("k") is None and cache.stats()["size"] == 0
