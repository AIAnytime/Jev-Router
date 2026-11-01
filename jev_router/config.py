from functools import lru_cache

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

STRATEGIES = ("balanced", "quality", "cheapest", "fastest", "privacy")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    jev_api_key: str = ""
    jev_base_url: str = "https://api.typesafe.ai/v1"
    jev_model: str = "jev-latest"
    jev_timeout: float = 8.0

    jev_router_api_keys: str = ""
    jev_router_config_dir: str = "config"
    database_url: str = "sqlite+aiosqlite:///./data/jev_router.db"

    default_strategy: str = "balanced"
    confidence_threshold: float = 0.55
    max_fallbacks: int = 3
    request_timeout: float = 120.0
    rate_limit_rpm: int = 120
    decision_cache_ttl: int = 600
    decision_cache_size: int = 2048
    log_prompts: bool = True
    cors_origins: str = "*"

    @property
    def api_keys(self) -> set[str]:
        return {k.strip() for k in self.jev_router_api_keys.split(",") if k.strip()}


@lru_cache
def get_settings() -> Settings:
    # Provider keys are resolved from os.environ by the registry, so .env must populate it.
    load_dotenv(override=False)
    return Settings()
