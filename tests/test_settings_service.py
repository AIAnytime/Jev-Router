import asyncio
from pathlib import Path

import pytest

from jev_router.api.deps import APIError
from jev_router.crypto import SecretBox
from jev_router.engine.cache import TTLCache
from jev_router.registry import Registry
from jev_router.services.settings_service import SettingsService
from jev_router.settings_store import SettingsStore
from jev_router.storage import TraceStore

ROOT = Path(__file__).resolve().parent.parent
LAB = {"name": "lab", "type": "openai", "base_url": "http://localhost:1234/v1", "api_key": "lab-key-0001",
       "headers": {"X-Team": "core"}, "local": True, "enabled": True}


def run(tmp_path, scenario, secret="s"):
    async def go():
        traces = TraceStore(f"sqlite+aiosqlite:///{tmp_path}/s.db")
        await traces.init()
        cache = TTLCache(16, 60)
        svc = SettingsService(Registry.load(ROOT / "config"), SettingsStore(traces.engine),
                              SecretBox.from_settings(secret, tmp_path / "k"), cache)
        await svc.load()
        try:
            return await scenario(svc, cache)
        finally:
            await traces.close()
    return asyncio.run(go())


async def status_of(coro):
    with pytest.raises(APIError) as e:
        await coro
    return e.value.status, e.value.code


def test_add_provider_and_models_goes_live(tmp_path, provider_env):
    async def scenario(svc, cache):
        cache.set("k", 1)
        await svc.add_provider(LAB)
        ids = await svc.add_models([{"provider": "lab", "model": "qwen3", "quality": 7.0},
                                    {"provider": "lab", "model": "phi4"}])
        reg = svc.registry
        assert ids == ["lab/qwen3", "lab/phi4"]
        assert reg.providers["lab"].api_key == "lab-key-0001" and reg.providers["lab"].headers == {"X-Team": "core"}
        qwen = reg.get_model("lab/qwen3")
        assert qwen.quality == 7.0 and qwen.local and reg.is_available(qwen)
        assert reg.get_model("lab/phi4").description == "phi4 served by lab"
        assert cache.get("k") is None
        assert svc.is_custom_model("lab/qwen3") and not svc.is_custom_model("liquid/lfm3-1.7b")
    run(tmp_path, scenario)


def test_errors_leave_everything_unchanged(tmp_path, provider_env):
    async def scenario(svc, cache):
        before = [m.id for m in svc.registry.models]
        assert await status_of(svc.add_provider({**LAB, "name": "openai"})) == (409, "conflict")
        assert await status_of(svc.update_provider("ghost", {"enabled": True})) == (404, "not_found")
        assert await status_of(svc.update_provider("openai", {"headers": {"X-A": "b"}})) == (400, "invalid_request")
        assert await status_of(svc.add_models([{"provider": "liquid", "model": "lfm3-1.7b"}])) == (409, "conflict")
        assert await status_of(svc.add_models([{"provider": "ghost", "model": "x"}])) == (400, "invalid_request")
        assert await status_of(svc.add_models([{"provider": "liquid", "model": "ok"},
                                               {"provider": "liquid", "model": "bad", "quality": "high"}])) \
            == (422, "invalid_settings")
        assert await status_of(svc.update_model("ghost/m", {"speed": 2.0})) == (404, "not_found")
        assert await status_of(svc.delete_model("ghost/m")) == (404, "not_found")
        assert [m.id for m in svc.registry.models] == before
        assert await svc.store.load() == svc.overlay
        assert svc.registry.get_model("liquid/ok") is None
    run(tmp_path, scenario)


def test_overrides_prune_and_reset(tmp_path, provider_env):
    async def scenario(svc, cache):
        await svc.update_model("liquid/lfm3-1.7b", {"speed": 4.0, "strengths": ["chat"]})
        assert svc.overridden("liquid/lfm3-1.7b") == ["speed", "strengths"]
        assert svc.registry.get_model("liquid/lfm3-1.7b").speed == 4.0
        await svc.update_model("liquid/lfm3-1.7b", {"speed": 9.0})
        assert svc.overridden("liquid/lfm3-1.7b") == ["strengths"]
        await svc.delete_model("liquid/lfm3-1.7b")
        assert svc.overridden("liquid/lfm3-1.7b") == []
        await svc.update_provider("ollama", {"enabled": True})
        assert svc.registry.providers["ollama"].available
        await svc.update_provider("ollama", {"base_url": "http://localhost:11434/v1"})
        assert svc.overlay.provider_overrides["ollama"].base_url is None
        await svc.delete_provider("ollama")
        assert "ollama" not in svc.overlay.provider_overrides and not svc.registry.providers["ollama"].available
    run(tmp_path, scenario)


def test_delete_custom_provider_cascades_and_state_persists(tmp_path, provider_env):
    async def first(svc, cache):
        await svc.add_provider(LAB)
        await svc.add_models([{"provider": "lab", "model": "qwen3"}])
        await svc.add_models([{"provider": "liquid", "model": "lfm-extra"}])
        await svc.update_provider("groq", {"api_key": "gsk-0001"})

    async def second(svc, cache):
        assert svc.registry.get_model("lab/qwen3") is not None
        assert svc.registry.providers["groq"].key_status == "settings"
        await svc.delete_provider("lab")
        assert "lab" not in svc.registry.providers and svc.registry.get_model("lab/qwen3") is None
        assert svc.registry.get_model("liquid/lfm-extra") is not None

    async def lost(svc, cache):
        assert svc.registry.providers["groq"].key_status == "unreadable"
        assert any("groq" in w for w in svc.warnings)

    run(tmp_path, first)
    run(tmp_path, second)
    run(tmp_path, lost, secret="a-different-secret")
