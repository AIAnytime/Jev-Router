import asyncio
import copy
import json
import logging
from collections.abc import Callable
from typing import Any

from ..api.deps import APIError
from ..crypto import SecretBox
from ..engine.cache import TTLCache
from ..overlay import CustomProvider, Overlay, ProviderOverride, merge
from ..registry import Registry
from ..settings_store import SettingsStore

log = logging.getLogger("jev_router")


def _not_found(kind: str, name: str) -> APIError:
    return APIError(404, f"{kind} '{name}' not found.", "not_found")


class SettingsService:
    def __init__(self, registry: Registry, store: SettingsStore, box: SecretBox, cache: TTLCache):
        self.registry = registry
        self.store = store
        self.box = box
        self.cache = cache
        self.overlay = Overlay()
        self.warnings: list[str] = []
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        self.overlay = await self.store.load()
        result = merge(self.registry.base_providers, self.registry.base_models, self.overlay, self.box)
        self.warnings = result.warnings + result.errors
        try:
            self.registry.apply(result.providers, result.models)
        except ValueError as e:
            self.warnings.append(f"saved settings were not applied ({e}); using models.yaml only")

    async def _update(self, mutate: Callable[[Overlay], None], message: str) -> None:
        async with self._lock:
            overlay = copy.deepcopy(self.overlay)
            mutate(overlay)
            result = merge(self.registry.base_providers, self.registry.base_models, overlay, self.box)
            if result.errors:
                raise APIError(422, result.errors[0], "invalid_settings")
            try:
                Registry.validate(result.providers, result.models)
            except ValueError as e:
                raise APIError(422, str(e), "invalid_settings") from None
            await self.store.save(overlay)
            self.overlay, self.warnings = overlay, result.warnings
            self.registry.apply(result.providers, result.models)
            self.cache.clear()
            log.info("settings: %s", message)

    def _enc(self, value: str | None) -> str | None:
        return self.box.encrypt(value) if value else None

    def _enc_headers(self, headers: dict[str, str] | None) -> str | None:
        return self.box.encrypt(json.dumps(headers)) if headers else None

    # -- providers ---------------------------------------------------------
    async def add_provider(self, data: dict[str, Any]) -> None:
        name = data["name"]

        def mutate(o: Overlay) -> None:
            if name in self.registry.base_providers or name in o.custom_providers:
                raise APIError(409, f"Provider '{name}' already exists.", "conflict")
            o.custom_providers[name] = CustomProvider(
                name=name, base_url=data["base_url"], type=data.get("type", "openai"),
                enabled=data.get("enabled", True), local=bool(data.get("local")),
                api_key_enc=self._enc(data.get("api_key")), headers_enc=self._enc_headers(data.get("headers")),
            )

        await self._update(mutate, f"provider {name} added")

    async def update_provider(self, name: str, changes: dict[str, Any]) -> None:
        def mutate(o: Overlay) -> None:
            if name in o.custom_providers:
                c = o.custom_providers[name]
                for f in ("type", "base_url", "enabled", "local"):
                    if f in changes:
                        setattr(c, f, changes[f])
                if "api_key" in changes:
                    c.api_key_enc = self._enc(changes["api_key"])
                if "headers" in changes:
                    c.headers_enc = self._enc_headers(changes["headers"])
                return
            base = self.registry.base_providers.get(name)
            if base is None:
                raise _not_found("Provider", name)
            extra = changes.keys() - {"api_key", "base_url", "enabled"}
            if extra:
                raise APIError(400, f"Built-in providers cannot change: {', '.join(sorted(extra))}.", "invalid_request")
            ov = o.provider_overrides.setdefault(name, ProviderOverride())
            if "base_url" in changes:
                ov.base_url = None if changes["base_url"] == base.base_url else changes["base_url"]
            if "enabled" in changes:
                ov.enabled = changes["enabled"]
            if "api_key" in changes:
                ov.api_key_enc = self._enc(changes["api_key"])
            if ov.is_empty():
                del o.provider_overrides[name]

        await self._update(mutate, f"provider {name} updated ({', '.join(sorted(changes))})")

    async def delete_provider(self, name: str) -> None:
        def mutate(o: Overlay) -> None:
            if name in o.custom_providers:
                del o.custom_providers[name]
                for mid in [k for k, v in o.custom_models.items() if v["provider"] == name]:
                    del o.custom_models[mid]
            elif name in self.registry.base_providers:
                o.provider_overrides.pop(name, None)
            else:
                raise _not_found("Provider", name)

        await self._update(mutate, f"provider {name} deleted")

    # -- models ------------------------------------------------------------
    @staticmethod
    def model_defaults(provider: str, model: str) -> dict[str, Any]:
        return {"description": f"{model} served by {provider}", "quality": 5.0, "speed": 5.0,
                "context_window": 32768, "input_cost": 0.0, "output_cost": 0.0, "modalities": ["text"]}

    def is_custom_model(self, model_id: str) -> bool:
        return model_id in self.overlay.custom_models

    def overridden(self, model_id: str) -> list[str]:
        return sorted(self.overlay.model_overrides.get(model_id, {}))

    async def add_models(self, items: list[dict[str, Any]]) -> list[str]:
        ids = [f"{i['provider']}/{i['model']}" for i in items]
        base_ids = {m.id for m in self.registry.base_models}

        def mutate(o: Overlay) -> None:
            seen: set[str] = set()
            for mid, item in zip(ids, items):
                if item["provider"] not in self.registry.providers:
                    raise APIError(400, f"Unknown provider '{item['provider']}'.", "invalid_request")
                if mid in seen or mid in base_ids or mid in o.custom_models:
                    raise APIError(409, f"Model '{mid}' already exists.", "conflict")
                seen.add(mid)
                o.custom_models[mid] = {**self.model_defaults(item["provider"], item["model"]), **item}

        await self._update(mutate, f"{len(ids)} model(s) added")
        return ids

    async def update_model(self, model_id: str, changes: dict[str, Any]) -> None:
        def mutate(o: Overlay) -> None:
            if model_id in o.custom_models:
                o.custom_models[model_id].update(changes)
                return
            base = next((m for m in self.registry.base_models if m.id == model_id), None)
            if base is None:
                raise _not_found("Model", model_id)
            fields = {**o.model_overrides.get(model_id, {}), **changes}
            fields = {k: v for k, v in fields.items() if getattr(base, k) != v}
            if fields:
                o.model_overrides[model_id] = fields
            else:
                o.model_overrides.pop(model_id, None)

        await self._update(mutate, f"model {model_id} updated ({', '.join(sorted(changes))})")

    async def delete_model(self, model_id: str) -> None:
        def mutate(o: Overlay) -> None:
            if model_id in o.custom_models:
                del o.custom_models[model_id]
            elif any(m.id == model_id for m in self.registry.base_models):
                o.model_overrides.pop(model_id, None)
            else:
                raise _not_found("Model", model_id)

        await self._update(mutate, f"model {model_id} reset")
