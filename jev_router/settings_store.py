import json
import logging
import time

from sqlalchemy import Boolean, Column, Float, String, Table, Text, delete, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine

from .overlay import CustomProvider, Overlay, ProviderOverride
from .storage import metadata

provider_overrides = Table(
    "provider_overrides", metadata,
    Column("name", String(32), primary_key=True),
    Column("base_url", Text),
    Column("enabled", Boolean),
    Column("api_key_enc", Text),
    Column("updated_at", Float, nullable=False),
)
custom_providers = Table(
    "custom_providers", metadata,
    Column("name", String(32), primary_key=True),
    Column("type", String(20), nullable=False),
    Column("base_url", Text, nullable=False),
    Column("enabled", Boolean, nullable=False),
    Column("local", Boolean, nullable=False),
    Column("api_key_enc", Text),
    Column("headers_enc", Text),
    Column("updated_at", Float, nullable=False),
)
model_overrides = Table(
    "model_overrides", metadata,
    Column("id", String(200), primary_key=True),
    Column("fields", Text, nullable=False),
    Column("updated_at", Float, nullable=False),
)
custom_models = Table(
    "custom_models", metadata,
    Column("id", String(200), primary_key=True),
    Column("provider", String(32), nullable=False),
    Column("spec", Text, nullable=False),
    Column("updated_at", Float, nullable=False),
)


def _decode(model_id: str, text: str) -> dict | None:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    if not isinstance(value, dict):
        logging.getLogger("jev_router").warning("settings: saved row for model '%s' is unreadable; skipped", model_id)
        return None
    return value


class SettingsStore:
    def __init__(self, engine: AsyncEngine):
        self.engine = engine

    async def load(self) -> Overlay:
        o = Overlay()
        async with self.engine.connect() as conn:
            for r in (await conn.execute(select(provider_overrides))).mappings():
                o.provider_overrides[r["name"]] = ProviderOverride(r["base_url"], r["enabled"], r["api_key_enc"])
            for r in (await conn.execute(select(custom_providers))).mappings():
                o.custom_providers[r["name"]] = CustomProvider(
                    name=r["name"], base_url=r["base_url"], type=r["type"], enabled=r["enabled"], local=r["local"],
                    api_key_enc=r["api_key_enc"], headers_enc=r["headers_enc"],
                )
            for r in (await conn.execute(select(model_overrides))).mappings():
                if (fields := _decode(r["id"], r["fields"])) is not None:
                    o.model_overrides[r["id"]] = fields
            for r in (await conn.execute(select(custom_models))).mappings():
                if (spec := _decode(r["id"], r["spec"])) is not None:
                    o.custom_models[r["id"]] = {"provider": r["provider"], **spec}
        return o

    async def save(self, overlay: Overlay) -> None:
        now = time.time()
        rows = {
            provider_overrides: [
                {"name": n, "base_url": o.base_url, "enabled": o.enabled, "api_key_enc": o.api_key_enc, "updated_at": now}
                for n, o in overlay.provider_overrides.items()
            ],
            custom_providers: [
                {"name": c.name, "type": c.type, "base_url": c.base_url, "enabled": c.enabled, "local": c.local,
                 "api_key_enc": c.api_key_enc, "headers_enc": c.headers_enc, "updated_at": now}
                for c in overlay.custom_providers.values()
            ],
            model_overrides: [
                {"id": mid, "fields": json.dumps(f), "updated_at": now} for mid, f in overlay.model_overrides.items()
            ],
            custom_models: [
                {"id": mid, "provider": spec["provider"],
                 "spec": json.dumps({k: v for k, v in spec.items() if k != "provider"}), "updated_at": now}
                for mid, spec in overlay.custom_models.items()
            ],
        }
        async with self.engine.begin() as conn:
            for table, values in rows.items():
                await conn.execute(delete(table))
                if values:
                    await conn.execute(insert(table), values)
