import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .config import STRATEGIES

TRUTHY = {"1", "true", "yes", "on"}


class ProviderSpec(BaseModel):
    name: str
    type: Literal["openai", "anthropic"] = "openai"
    base_url: str
    api_key_env: str | None = None
    requires_key: bool = True
    enabled_env: str | None = None
    headers: dict[str, str] = Field(default_factory=dict, repr=False)
    local: bool = False
    source: Literal["builtin", "custom"] = "builtin"
    stored_key: str | None = Field(None, repr=False, exclude=True)
    stored_key_unreadable: bool = False
    enabled_override: bool | None = None

    @property
    def env_key(self) -> str | None:
        return (os.environ.get(self.api_key_env) or None) if self.api_key_env else None

    @property
    def api_key(self) -> str | None:
        return self.stored_key or self.env_key

    @property
    def key_status(self) -> Literal["settings", "unreadable", "env"] | None:
        if self.stored_key:
            return "settings"
        if self.stored_key_unreadable:
            return "unreadable"
        if self.env_key:
            return "env"
        return None

    @property
    def enabled(self) -> bool:
        if self.enabled_override is not None:
            return self.enabled_override
        return not self.enabled_env or os.environ.get(self.enabled_env, "").lower() in TRUTHY

    @property
    def available(self) -> bool:
        return self.enabled and (not self.requires_key or bool(self.api_key))


class ModelSpec(BaseModel):
    id: str
    provider: str
    model: str
    description: str
    strengths: list[str] = Field(default_factory=list)
    quality: float = 5.0
    speed: float = 5.0
    input_cost: float = 0.0
    output_cost: float = 0.0
    context_window: int = 32768
    max_output_tokens: int | None = None
    modalities: list[str] = Field(default_factory=lambda: ["text"])
    only_when: list[str] = Field(default_factory=list)
    supports_tools: bool = True
    supports_json: bool = True
    local: bool = False
    reasoning: bool = False
    enabled: bool = True

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_cost + output_tokens * self.output_cost) / 1_000_000

    def jev_description(self) -> str:
        parts = [self.description]
        if self.strengths:
            parts.append("Strengths: " + ", ".join(self.strengths) + ".")
        parts.append(f"Capability {self.quality:g}/10.")
        return " ".join(parts)


class AgentSpec(BaseModel):
    name: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    requires_approval: bool = False

    def jev_description(self) -> str:
        text = self.description
        if self.capabilities:
            text += " Capabilities: " + ", ".join(self.capabilities) + "."
        return text


class GuardrailCheck(BaseModel):
    instructions: str
    review: float | None = None
    deny: float | None = None


class GuardrailConfig(BaseModel):
    always_on: bool = False
    block_on_review: bool = False
    checks: dict[str, GuardrailCheck] = Field(default_factory=dict)


class RouterConfig(BaseModel):
    strategies: dict[str, dict[str, float]]
    min_capability: float = 0.35
    guardrails: GuardrailConfig = Field(default_factory=GuardrailConfig)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


class Registry:
    def __init__(
        self,
        providers: dict[str, ProviderSpec],
        models: list[ModelSpec],
        agents: list[AgentSpec],
        router: RouterConfig,
    ):
        self.validate(providers, models)
        missing = set(STRATEGIES) - router.strategies.keys()
        if missing:
            raise ValueError(f"router.yaml is missing strategies: {sorted(missing)}")
        self.providers = providers
        self.models = models
        self.agents = agents
        self.router = router
        self.base_providers = {name: p.model_copy(deep=True) for name, p in providers.items()}
        self.base_models = [m.model_copy(deep=True) for m in models]

    @staticmethod
    def validate(providers: dict[str, ProviderSpec], models: list[ModelSpec]) -> None:
        unknown = {m.provider for m in models} - providers.keys()
        if unknown:
            raise ValueError(f"models reference unknown providers: {sorted(unknown)}")
        seen: set[str] = set()
        dupes = sorted({m.id for m in models if m.id in seen or seen.add(m.id)})
        if dupes:
            raise ValueError(f"duplicate model ids: {dupes}")

    def apply(self, providers: dict[str, ProviderSpec], models: list[ModelSpec]) -> None:
        self.validate(providers, models)
        self.providers = providers
        self.models = models

    @classmethod
    def load(cls, config_dir: str | Path) -> "Registry":
        root = Path(config_dir)
        models_cfg = _read_yaml(root / "models.yaml")
        providers = {
            name: ProviderSpec(name=name, **spec)
            for name, spec in (models_cfg.get("providers") or {}).items()
        }
        models = [ModelSpec(**m) for m in models_cfg.get("models") or []]
        agents = [AgentSpec(**a) for a in _read_yaml(root / "agents.yaml").get("agents") or []]
        router = RouterConfig(**_read_yaml(root / "router.yaml"))
        return cls(providers, models, agents, router)

    def provider_for(self, model: ModelSpec) -> ProviderSpec:
        return self.providers[model.provider]

    def is_available(self, model: ModelSpec) -> bool:
        return model.enabled and self.provider_for(model).available

    def available_models(self) -> list[ModelSpec]:
        return [m for m in self.models if self.is_available(m)]

    def get_model(self, name: str) -> ModelSpec | None:
        for m in self.models:
            if m.id == name:
                return m
        for m in self.models:
            if m.model == name:
                return m
        return None
