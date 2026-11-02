import asyncio

from sqlalchemy import update

from jev_router.crypto import SecretBox
from jev_router.overlay import CustomProvider, Overlay, ProviderOverride
from jev_router.settings_store import SettingsStore, custom_models, model_overrides
from jev_router.storage import TraceStore


def with_store(tmp_path, fn):
    async def go():
        traces = TraceStore(f"sqlite+aiosqlite:///{tmp_path}/s.db")
        await traces.init()
        try:
            return await fn(SettingsStore(traces.engine))
        finally:
            await traces.close()
    return asyncio.run(go())


def sample(box):
    return Overlay(
        provider_overrides={"openai": ProviderOverride(enabled=True, api_key_enc=box.encrypt("sk-plaintext-openai-1"))},
        custom_providers={"lab": CustomProvider(
            name="lab", base_url="http://localhost:1234/v1", local=True,
            api_key_enc=box.encrypt("lab-plaintext-key"), headers_enc=box.encrypt('{"X-Team": "plaintext-header"}'))},
        model_overrides={"liquid/lfm3-1.7b": {"speed": 3.0, "strengths": ["chat"]}},
        custom_models={"lab/qwen3": {"provider": "lab", "model": "qwen3", "description": "Qwen", "quality": 6.0}},
    )


def test_round_trip_and_rewrite(tmp_path):
    overlay = sample(SecretBox.from_settings("s", tmp_path / "k"))

    async def fn(store):
        empty = await store.load()
        await store.save(overlay)
        first = await store.load()
        await store.save(Overlay())
        return empty, first, await store.load()

    empty, first, second = with_store(tmp_path, fn)
    assert empty == Overlay() and first == overlay and second == Overlay()


def test_secrets_are_not_stored_in_plaintext(tmp_path):
    box = SecretBox.from_settings("s", tmp_path / "k")
    with_store(tmp_path, lambda store: store.save(sample(box)))
    raw = (tmp_path / "s.db").read_bytes()
    assert b"lab/qwen3" in raw
    for secret in (b"sk-plaintext-openai-1", b"lab-plaintext-key", b"plaintext-header"):
        assert secret not in raw


def test_unreadable_model_rows_are_skipped(tmp_path, caplog):
    overlay = sample(SecretBox.from_settings("s", tmp_path / "k"))
    overlay.model_overrides["liquid/lfm2.5-24b-a2b"] = {"speed": 4.0}
    overlay.custom_models["lab/broken"] = {"provider": "lab", "model": "broken", "description": "B"}

    async def fn(store):
        await store.save(overlay)
        async with store.engine.begin() as conn:
            for table, mid, values in ((model_overrides, "liquid/lfm3-1.7b", {"fields": "{not json"}),
                                       (model_overrides, "liquid/lfm2.5-24b-a2b", {"fields": "[1]"}),
                                       (custom_models, "lab/broken", {"spec": "null"})):
                await conn.execute(update(table).where(table.c.id == mid).values(**values))
        return await store.load()

    loaded = with_store(tmp_path, fn)
    assert loaded.model_overrides == {}
    assert loaded.custom_models == {"lab/qwen3": overlay.custom_models["lab/qwen3"]}
    assert loaded.custom_providers == overlay.custom_providers
    assert "saved row for model 'liquid/lfm3-1.7b' is unreadable; skipped" in caplog.text
    assert "'lab/broken'" in caplog.text
