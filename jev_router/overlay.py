import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from .crypto import SecretBox
from .registry import ModelSpec, ProviderSpec

EDITABLE_MODEL_FIELDS = (
    "description", "strengths", "quality", "speed", "input_cost", "output_cost", "context_window",
    "max_output_tokens", "modalities", "only_when", "supports_tools", "supports_json", "reasoning", "enabled",
)


@dataclass
class ProviderOverride:
    base_url: str | None = None
    enabled: bool | None = None
    api_key_enc: str | None = None

    def is_empty(self) -> bool:
        return self.base_url is None and self.enabled is None and self.api_key_enc is None


@dataclass
class CustomProvider:
    name: str
    base_url: str
    type: str = "openai"
    enabled: bool = True
    local: bool = False
    api_key_enc: str | None = None
    headers_enc: str | None = None


@dataclass
class Overlay:
    provider_overrides: dict[str, ProviderOverride] = field(default_factory=dict)
    custom_providers: dict[str, CustomProvider] = field(default_factory=dict)
    model_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    custom_models: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class MergeResult:
    providers: dict[str, ProviderSpec]
    models: list[ModelSpec]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _first_error(e: ValidationError) -> str:
    err = e.errors()[0]
    return f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}"


def _open(box: SecretBox, token: str | None, what: str, warnings: list[str]) -> tuple[str | None, bool]:
    if token is None:
        return None, False
    value = box.decrypt(token)
    if value is None:
        warnings.append(f"{what} could not be decrypted; re-enter it in Settings")
        return None, True
    return value, False


def merge(
    base_providers: dict[str, ProviderSpec],
    base_models: list[ModelSpec],
    overlay: Overlay,
    box: SecretBox,
) -> MergeResult:
    warnings: list[str] = []
    errors: list[str] = []
    providers = {name: p.model_copy(deep=True) for name, p in base_providers.items()}

    for name, o in sorted(overlay.provider_overrides.items()):
        p = providers.get(name)
        if p is None:
            warnings.append(f"settings for provider '{name}' ignored: it is no longer in models.yaml")
            continue
        if o.base_url is not None:
            p.base_url = o.base_url
        p.enabled_override = o.enabled
        p.stored_key, p.stored_key_unreadable = _open(box, o.api_key_enc, f"saved key for provider '{name}'", warnings)

    for name, c in sorted(overlay.custom_providers.items()):
        if name in providers:
            warnings.append(f"custom provider '{name}' skipped: models.yaml now defines a provider with that name")
            continue
        key, unreadable = _open(box, c.api_key_enc, f"saved key for provider '{name}'", warnings)
        raw_headers, _ = _open(box, c.headers_enc, f"saved headers for provider '{name}'", warnings)
        try:
            providers[name] = ProviderSpec(
                name=name, type=c.type, base_url=c.base_url, requires_key=False, local=c.local, source="custom",
                enabled_override=c.enabled, stored_key=key, stored_key_unreadable=unreadable,
                headers=json.loads(raw_headers) if raw_headers else {},
            )
        except ValidationError as e:
            errors.append(f"provider '{name}': {_first_error(e)}")

    models: list[ModelSpec] = []
    base_ids = {m.id for m in base_models}
    for m in base_models:
        fields = overlay.model_overrides.get(m.id)
        if not fields:
            models.append(m.model_copy(deep=True))
            continue
        try:
            models.append(ModelSpec(**{**m.model_dump(), **fields}))
        except ValidationError as e:
            errors.append(f"model '{m.id}': {_first_error(e)}")
    for mid in sorted(overlay.model_overrides.keys() - base_ids):
        warnings.append(f"settings for model '{mid}' ignored: it is no longer in models.yaml")

    for mid, spec in sorted(overlay.custom_models.items()):
        if mid in base_ids:
            warnings.append(f"custom model '{mid}' skipped: models.yaml now defines a model with that id")
            continue
        if spec.get("provider") not in providers:
            warnings.append(f"custom model '{mid}' skipped: provider '{spec.get('provider')}' is missing")
            continue
        try:
            models.append(ModelSpec(id=mid, **spec))
        except ValidationError as e:
            errors.append(f"model '{mid}': {_first_error(e)}")

    for m in models:
        if providers[m.provider].local:
            m.local = True
    return MergeResult(providers, models, warnings, errors)
