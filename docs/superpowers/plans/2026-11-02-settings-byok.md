# Settings (BYOK, custom providers, local models) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator manage provider keys, custom OpenAI-compatible providers (Ollama, LM Studio, vLLM, ...) and model routing metadata from a Settings API and dashboard page, applied live without a restart.

**Architecture:** `config/models.yaml` stays the base. A database overlay (four new tables, keys encrypted with Fernet) stores only changes. A pure `merge()` combines base and overlay; `SettingsService` validates, commits, swaps the result into the one shared `Registry` instance under a lock and clears the decision cache. A loopback-or-admin-key guarded FastAPI router exposes `/v1/settings/*`; the single-file dashboard gains a fifth "Settings" view.

**Tech Stack:** Python 3.12, FastAPI, pydantic 2, async SQLAlchemy + aiosqlite, httpx, `cryptography` (Fernet), vanilla JS dashboard (`jev_router/dashboard/index.html`).

**Spec:** `docs/superpowers/specs/2026-11-02-settings-byok-design.md`

## Global Constraints

- Test command: `.venv/bin/python -m pytest -q -p no:asyncio` (no pytest-asyncio; async code in tests runs through `asyncio.run`).
- New dependency: `cryptography>=42` only. Install with `.venv/bin/pip install 'cryptography>=42'`.
- Secrets (API keys, header values) are write-only: never returned by any endpoint, never logged, never put in traces or error messages. Logs name changed fields only.
- Key hint: first 3 + `…` + last 4 characters when the key has at least 12 characters, otherwise `…` + last 2. Header values are shown as `••••`.
- Master secret: `JEV_ROUTER_SECRET` (any string, Fernet key = urlsafe base64 of its SHA-256) or a generated `data/secret.key` with mode `0600`.
- Settings access: `JEV_ROUTER_ADMIN_KEY` set → required (`Authorization: Bearer` or `x-api-key`, `hmac.compare_digest`). Unset → client host must be `127.0.0.1` or `::1` and the request must carry no `X-Forwarded-For` / `Forwarded`. Router keys never grant access. Failure: `403 settings_forbidden`.
- Error envelope: `{"error": {"message", "type", "code", ...}}` via `APIError` in `jev_router/api/deps.py`. Codes: `400 invalid_request`, `403 settings_forbidden`, `404 not_found`, `409 conflict`, `422 invalid_settings`, `502 discovery_failed`.
- Provider name: `^[a-z0-9][a-z0-9_-]{0,31}$`. `base_url`: http/https with a host, trailing slash stripped. Headers: at most 20, names `^[A-Za-z0-9-]{1,64}$`, values up to 2048 characters, `Authorization` rejected.
- Model fields: `quality`, `speed` in `[1, 10]`; costs `>= 0`; `context_window > 0`; `modalities` ⊆ `{text, image, audio}`. Added-model defaults: `quality 5, speed 5, context_window 32768, input_cost 0, output_cost 0, modalities [text], description "{model} served by {provider}"`.
- Discovery: timeout 5 s, `follow_redirects=False`, body capped at 1 MB, at most 500 models, error text trimmed to 300 characters and scrubbed of secrets.
- Never commit `.env` or `idea.md`. Stage files by name. Ask the user before every commit and push.
- Port 8000 belongs to another project: never kill or bind it. Run this app on `:8787` for manual checks.
- UI copy: plain functional labels, no taglines; reuse the existing dashboard classes and CSS variables.

## File Structure

| File | Status | Responsibility |
| --- | --- | --- |
| `jev_router/crypto.py` | create | `SecretBox` (Fernet wrapper, master secret resolution), `mask()` |
| `jev_router/registry.py` | modify | `ProviderSpec` key precedence / enabled override / local / source; `Registry.validate`, `base_providers`, `base_models`, `apply` |
| `jev_router/engine/cache.py` | modify | `TTLCache.clear()` |
| `jev_router/overlay.py` | create | Overlay dataclasses and pure `merge()` |
| `jev_router/settings_store.py` | create | Four overlay tables; `SettingsStore.load()` / `save()` |
| `jev_router/services/settings_service.py` | create | Locked validate → commit → apply → clear-cache pipeline and domain operations |
| `jev_router/services/discovery.py` | create | `list_models()`, `probe()` against provider endpoints |
| `jev_router/api/settings.py` | create | `/v1/settings/*` router, request bodies, access guard, export |
| `jev_router/api/chat.py` | modify | `/v1/models` gains `enabled` |
| `jev_router/config.py` | modify | `jev_router_admin_key`, `jev_router_secret`, `jev_router_secret_file` |
| `jev_router/main.py` | modify | Wire `SecretBox`, `SettingsStore`, `SettingsService`, `app.state.http`, settings router |
| `config/models.yaml` | modify | `ollama` provider gets `local: true` |
| `jev_router/dashboard/index.html` | modify | Settings view, nav item, drawer reuse, hints elsewhere |
| `tests/conftest.py` | modify | `client_host`, secret settings, GET listing support and request capture in `FakeUpstream` |
| `tests/test_crypto.py`, `tests/test_registry.py`, `tests/test_overlay.py`, `tests/test_settings_store.py`, `tests/test_settings_service.py`, `tests/test_discovery.py`, `tests/test_settings_api.py` | create | Tests per unit |
| `pyproject.toml`, `README.md`, `.env.example` | modify | Dependency and docs |

---

### Task 1: Config fields and SecretBox

**Files:**
- Modify: `pyproject.toml:14-24` (dependencies list)
- Modify: `jev_router/config.py:17-19`
- Create: `jev_router/crypto.py`
- Test: `tests/test_crypto.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `Settings.jev_router_admin_key: str = ""`, `Settings.jev_router_secret: str = ""`, `Settings.jev_router_secret_file: str = "data/secret.key"`.
  - `SecretBox.from_settings(secret: str, key_file: str | Path) -> SecretBox`; attribute `source: Literal["env", "file"]`; `encrypt(value: str) -> str`; `decrypt(token: str) -> str | None`.
  - `mask(value: str) -> str`.

- [ ] **Step 1: Add and install the dependency**

In `pyproject.toml`, add one line after `"aiosqlite>=0.20",` inside `dependencies = [ ... ]`:

```toml
  "cryptography>=42",
```

Run: `.venv/bin/pip install 'cryptography>=42'`
Expected: `Successfully installed cryptography-...` (or "Requirement already satisfied").

Run: `git check-ignore data/secret.key`
Expected: prints `data/secret.key` (the `data/` rule in `.gitignore` covers the key file).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_crypto.py`:

```python
import stat

from jev_router.config import Settings
from jev_router.crypto import SecretBox, mask


def test_round_trip_with_env_secret(tmp_path):
    box = SecretBox.from_settings("any string at all", tmp_path / "unused.key")
    token = box.encrypt("sk-live-123")
    assert token != "sk-live-123"
    assert box.decrypt(token) == "sk-live-123"
    assert box.source == "env"
    assert not (tmp_path / "unused.key").exists()


def test_wrong_secret_or_garbage_returns_none(tmp_path):
    token = SecretBox.from_settings("one", tmp_path / "k").encrypt("value")
    other = SecretBox.from_settings("two", tmp_path / "k")
    assert other.decrypt(token) is None
    assert other.decrypt("not-a-token") is None


def test_key_file_is_generated_0600_and_reused(tmp_path):
    path = tmp_path / "sub" / "secret.key"
    box = SecretBox.from_settings("", path)
    assert box.source == "file"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert SecretBox.from_settings("", path).decrypt(box.encrypt("x")) == "x"


def test_mask():
    assert mask("sk-abcdefgh1234") == "sk-…1234"
    assert mask("short") == "…rt"


def test_settings_defaults():
    fields = Settings.model_fields
    assert fields["jev_router_secret_file"].default == "data/secret.key"
    assert fields["jev_router_admin_key"].default == "" and fields["jev_router_secret"].default == ""
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_crypto.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'jev_router.crypto'`.

- [ ] **Step 4: Add the settings fields**

In `jev_router/config.py`, after `database_url: str = "sqlite+aiosqlite:///./data/jev_router.db"` add:

```python
    jev_router_admin_key: str = ""
    jev_router_secret: str = ""
    jev_router_secret_file: str = "data/secret.key"
```

- [ ] **Step 5: Implement `jev_router/crypto.py`**

```python
import base64
import hashlib
import os
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: bytes, source: Literal["env", "file"]):
        self._fernet = Fernet(key)
        self.source = source

    @classmethod
    def from_settings(cls, secret: str, key_file: str | Path) -> "SecretBox":
        if secret:
            return cls(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()), "env")
        path = Path(key_file)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as f:
                    f.write(Fernet.generate_key())
        return cls(path.read_bytes().strip(), "file")

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            return None


def mask(value: str) -> str:
    return f"{value[:3]}…{value[-4:]}" if len(value) >= 12 else f"…{value[-2:]}"
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_crypto.py`
Expected: `5 passed`.

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: all tests pass (41 existing + 5 new).

- [ ] **Step 7: Commit (ask the user first)**

```bash
git add pyproject.toml jev_router/config.py jev_router/crypto.py tests/test_crypto.py
git commit -m "Add SecretBox for encrypting provider keys at rest"
```

---

### Task 2: Registry key precedence, enabled override, live apply

**Files:**
- Modify: `jev_router/registry.py:13-30` (`ProviderSpec`) and `:103-133` (`Registry.__init__`, new methods)
- Modify: `jev_router/engine/cache.py` (add `clear`)
- Modify: `config/models.yaml` (ollama provider)
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `ProviderSpec` new fields: `local: bool = False`, `source: Literal["builtin", "custom"] = "builtin"`, `stored_key: str | None` (excluded from dumps and repr), `stored_key_unreadable: bool = False`, `enabled_override: bool | None = None`.
  - `ProviderSpec` properties: `env_key -> str | None`, `api_key -> str | None` (stored, then env), `key_status -> "settings" | "unreadable" | "env" | None`, `enabled -> bool`, `available -> bool`.
  - `Registry.validate(providers: dict[str, ProviderSpec], models: list[ModelSpec]) -> None` (static; raises `ValueError` on unknown provider or duplicate model id).
  - `Registry.base_providers: dict[str, ProviderSpec]`, `Registry.base_models: list[ModelSpec]` (deep copies of the YAML state, never mutated).
  - `Registry.apply(providers, models) -> None` (validates, then swaps in place).
  - `TTLCache.clear() -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_registry.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_registry.py`
Expected: FAIL (e.g. `AttributeError: 'ProviderSpec' object has no attribute 'key_status'`, `ValidationError` for unknown field `stored_key`, `AttributeError: 'Registry' ... 'validate'`).

- [ ] **Step 3: Replace `ProviderSpec` in `jev_router/registry.py`**

```python
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
```

- [ ] **Step 4: Replace `Registry.__init__` and add `validate` / `apply`**

Replace the body of `Registry.__init__` (lines 104-120) with:

```python
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
```

`ModelRouter`, `Executor`, `PolicyEngine`, `cli.py` and `api/chat.py` all read `registry.providers` / `registry.models` through the shared instance on every call, so reassigning the attributes is enough.

- [ ] **Step 5: Add `TTLCache.clear`**

In `jev_router/engine/cache.py`, after `set`:

```python
    def clear(self) -> None:
        self._data.clear()
```

- [ ] **Step 6: Mark the Ollama provider local**

In `config/models.yaml`, the `ollama` provider block becomes:

```yaml
  ollama:
    type: openai
    base_url: http://localhost:11434/v1
    requires_key: false
    enabled_env: OLLAMA_ENABLED
    local: true
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_registry.py`
Expected: `6 passed`.

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: all pass (existing `OLLAMA_ENABLED` behaviour unchanged).

- [ ] **Step 8: Commit (ask the user first)**

```bash
git add jev_router/registry.py jev_router/engine/cache.py config/models.yaml tests/test_registry.py
git commit -m "Let the registry take stored keys, enable overrides and live swaps"
```

---

### Task 3: Overlay model and pure merge

**Files:**
- Create: `jev_router/overlay.py`
- Test: `tests/test_overlay.py`

**Interfaces:**
- Consumes: `SecretBox.decrypt`, `ProviderSpec` fields from Task 2, `ModelSpec`.
- Produces:
  - `EDITABLE_MODEL_FIELDS: tuple[str, ...]`.
  - `ProviderOverride(base_url: str | None = None, enabled: bool | None = None, api_key_enc: str | None = None)` with `is_empty() -> bool`.
  - `CustomProvider(name: str, base_url: str, type: str = "openai", enabled: bool = True, local: bool = False, api_key_enc: str | None = None, headers_enc: str | None = None)`; `headers_enc` is the encrypted JSON object of headers.
  - `Overlay(provider_overrides: dict[str, ProviderOverride], custom_providers: dict[str, CustomProvider], model_overrides: dict[str, dict[str, Any]], custom_models: dict[str, dict[str, Any]])`. `custom_models[id]` holds `provider`, `model` and every other `ModelSpec` field except `id`.
  - `MergeResult(providers: dict[str, ProviderSpec], models: list[ModelSpec], warnings: list[str], errors: list[str])`.
  - `merge(base_providers, base_models, overlay: Overlay, box: SecretBox) -> MergeResult`. Never mutates its inputs. Custom providers are built with `source="custom"`, `requires_key=False`. Models of a `local` provider get `local=True`. Problems with saved data become `warnings`; items that fail pydantic validation become `errors` and are left out.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_overlay.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_overlay.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'jev_router.overlay'`.

- [ ] **Step 3: Implement `jev_router/overlay.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_overlay.py`
Expected: `6 passed`.

- [ ] **Step 5: Commit (ask the user first)**

```bash
git add jev_router/overlay.py tests/test_overlay.py
git commit -m "Add the settings overlay and a pure merge over models.yaml"
```

---

### Task 4: Settings store (four overlay tables)

**Files:**
- Create: `jev_router/settings_store.py`
- Test: `tests/test_settings_store.py`

**Interfaces:**
- Consumes: `metadata` from `jev_router/storage.py` (tables register on it at import, so `TraceStore.init()` creates them), `TraceStore.engine`, overlay dataclasses from Task 3.
- Produces: `SettingsStore(engine: AsyncEngine)` with `async load() -> Overlay` and `async save(overlay: Overlay) -> None` (rewrites all four tables in one transaction).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_settings_store.py`:

```python
import asyncio

from jev_router.crypto import SecretBox
from jev_router.overlay import CustomProvider, Overlay, ProviderOverride
from jev_router.settings_store import SettingsStore
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_settings_store.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'jev_router.settings_store'`.

- [ ] **Step 3: Implement `jev_router/settings_store.py`**

```python
import json
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
                o.model_overrides[r["id"]] = json.loads(r["fields"])
            for r in (await conn.execute(select(custom_models))).mappings():
                o.custom_models[r["id"]] = {"provider": r["provider"], **json.loads(r["spec"])}
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_settings_store.py`
Expected: `2 passed`.

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: all pass.

- [ ] **Step 5: Commit (ask the user first)**

```bash
git add jev_router/settings_store.py tests/test_settings_store.py
git commit -m "Persist the settings overlay in four database tables"
```

---

### Task 5: Settings service (validate → commit → apply → clear cache)

**Files:**
- Create: `jev_router/services/settings_service.py`
- Modify: `jev_router/main.py:37-58` (lifespan wiring)
- Test: `tests/test_settings_service.py`

**Interfaces:**
- Consumes: `Registry.validate/apply/base_providers/base_models` (Task 2), `TTLCache.clear` (Task 2), `merge`, `Overlay`, `ProviderOverride`, `CustomProvider` (Task 3), `SettingsStore` (Task 4), `SecretBox` (Task 1), `APIError` from `jev_router/api/deps.py`.
- Produces: `SettingsService(registry, store, box, cache)` with:
  - attributes `overlay: Overlay`, `warnings: list[str]`, `box: SecretBox`.
  - `async load() -> None` (startup; never raises on bad saved data).
  - `async add_provider(data: dict) -> None` — keys `name, type, base_url, api_key, headers, local, enabled` (`local` already resolved to a bool by the API layer).
  - `async update_provider(name: str, changes: dict) -> None` — only keys the client sent; `api_key: None` clears the saved key, `headers: None` clears headers.
  - `async delete_provider(name: str) -> None`.
  - `async add_models(items: list[dict]) -> list[str]` — each item has `provider`, `model` and optional editable fields; returns the new ids `"{provider}/{model}"`.
  - `async update_model(model_id: str, changes: dict) -> None`, `async delete_model(model_id: str) -> None`.
  - `is_custom_model(model_id: str) -> bool`, `overridden(model_id: str) -> list[str]`.
  - `@staticmethod model_defaults(provider: str, model: str) -> dict`.
  - Errors: `APIError` 404 `not_found`, 409 `conflict`, 400 `invalid_request`, 422 `invalid_settings`.
  - `app.state.settings_service`, `app.state.http` (the shared `httpx.AsyncClient`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_settings_service.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_settings_service.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'jev_router.services.settings_service'`.

- [ ] **Step 3: Implement `jev_router/services/settings_service.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_settings_service.py`
Expected: `4 passed`.

- [ ] **Step 5: Wire the service into the app lifespan**

In `jev_router/main.py` add imports:

```python
from .crypto import SecretBox
from .services.settings_service import SettingsService
from .settings_store import SettingsStore
```

In `lifespan`, right after `decision = DecisionEngine(...)` add:

```python
        box = SecretBox.from_settings(settings.jev_router_secret, settings.jev_router_secret_file)
        settings_service = SettingsService(registry, SettingsStore(store.engine), box, decision.cache)
        await settings_service.load()
        for warning in settings_service.warnings:
            log.warning("settings: %s", warning)
```

and after `s.limiter = RateLimiter(...)`:

```python
        s.http, s.settings_service = http, settings_service
```

The import of `settings_store` must stay at module top so its tables are registered on `metadata` before `store.init()` runs `create_all`.

- [ ] **Step 6: Point the test client at a temporary secret**

In `tests/conftest.py`, add to the `values` dict in `make_client.factory`:

```python
            "jev_router_admin_key": "",
            "jev_router_secret": "test-secret",
            "jev_router_secret_file": str(tmp_path / "secret.key"),
```

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: all pass, and no `data/secret.key` is created in the repo by the test run (`ls data/secret.key` only exists if the app was run manually before).

- [ ] **Step 7: Commit (ask the user first)**

```bash
git add jev_router/services/settings_service.py jev_router/main.py tests/conftest.py tests/test_settings_service.py
git commit -m "Apply settings changes live through a locked validate-commit-swap service"
```

---

### Task 6: Model discovery and connection test

**Files:**
- Create: `jev_router/services/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: an `httpx.AsyncClient` (the app passes `app.state.http`).
- Produces:
  - `DiscoveryError(Exception)` whose message is already trimmed (≤ 300 characters) and scrubbed of the key and header values.
  - `async list_models(client, type_: str, base_url: str, api_key: str | None = None, headers: dict[str, str] | None = None) -> list[str]` — deduplicated, order kept, at most 500.
  - `async probe(client, type_, base_url, api_key=None, headers=None) -> dict` → `{"ok": bool, "latency_ms": float, "models_found": int, "error": str | None}`; never raises.
  - Constants `MAX_BYTES = 1_000_000`, `MAX_MODELS = 500`, `TIMEOUT = 5.0`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_discovery.py`:

```python
import asyncio

import httpx
import pytest

from jev_router.services.discovery import MAX_BYTES, DiscoveryError, list_models, probe


def call(handler, fn=list_models, type_="openai", base_url="http://localhost:1234/v1", **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fn(client, type_, base_url, **kw)
    return asyncio.run(go())


def test_openai_shape_with_auth_and_dedupe():
    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer k-123" and request.headers["x-team"] == "core"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"nope": 1}]})
    assert call(handler, api_key="k-123", headers={"X-Team": "core"}) == ["a", "b"]


def test_ollama_tags_fallback_after_404():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.2:3b"}, {"name": "qwen3:8b"}]})
        return httpx.Response(404, text="not found")
    assert call(handler, base_url="http://localhost:11434/v1/") == ["llama3.2:3b", "qwen3:8b"]
    assert seen == ["/v1/models", "/api/tags"]


def test_anthropic_headers():
    def handler(request):
        assert request.headers["x-api-key"] == "ak-1" and request.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"data": [{"id": "claude-x"}]})
    assert call(handler, type_="anthropic", base_url="https://api.anthropic.com/v1", api_key="ak-1") == ["claude-x"]


def test_caps_models_at_500():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": f"m{i}"} for i in range(600)]})
    assert len(call(handler)) == 500


@pytest.mark.parametrize("response,fragment", [
    (lambda r: httpx.Response(401, text="invalid key sk-secret-123456 for X-Team core-secret"), "HTTP 401"),
    (lambda r: httpx.Response(200, content=b"x" * (MAX_BYTES + 10)), "larger than 1 MB"),
    (lambda r: httpx.Response(200, text="<html>"), "Unexpected response"),
    (lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow", request=r)), "Timed out"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused", request=r)), "Connection failed"),
])
def test_failures_are_scrubbed_and_trimmed(response, fragment):
    with pytest.raises(DiscoveryError) as e:
        call(response, api_key="sk-secret-123456", headers={"X-Team": "core-secret"})
    message = str(e.value)
    assert fragment in message and len(message) <= 300
    assert "sk-secret-123456" not in message and "core-secret" not in message


def test_probe_reports_instead_of_raising():
    ok = call(lambda r: httpx.Response(200, json={"data": [{"id": "a"}]}), fn=probe)
    assert ok["ok"] and ok["models_found"] == 1 and ok["error"] is None and ok["latency_ms"] >= 0
    bad = call(lambda r: httpx.Response(500, text="boom"), fn=probe)
    assert bad["ok"] is False and bad["models_found"] == 0 and "HTTP 500" in bad["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_discovery.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'jev_router.services.discovery'`.

- [ ] **Step 3: Implement `jev_router/services/discovery.py`**

```python
import json
import time

import httpx

MAX_BYTES = 1_000_000
MAX_MODELS = 500
TIMEOUT = 5.0
ANTHROPIC_VERSION = "2023-06-01"


class DiscoveryError(Exception):
    pass


def _scrub(text: str, secrets: list[str | None]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "****")
    return text[:300]


def _request_headers(type_: str, api_key: str | None, headers: dict[str, str] | None) -> dict[str, str]:
    out = dict(headers or {})
    if type_ == "anthropic":
        out["anthropic-version"] = ANTHROPIC_VERSION
        if api_key:
            out["x-api-key"] = api_key
    elif api_key:
        out["Authorization"] = f"Bearer {api_key}"
    return out


async def _get(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    async with client.stream("GET", url, headers=headers, timeout=TIMEOUT, follow_redirects=False) as r:
        body = b""
        async for chunk in r.aiter_bytes():
            body += chunk
            if len(body) > MAX_BYTES:
                raise DiscoveryError("Response is larger than 1 MB.")
        return r.status_code, body


async def list_models(
    client: httpx.AsyncClient,
    type_: str,
    base_url: str,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
) -> list[str]:
    secrets = [api_key, *(headers or {}).values()]
    request_headers = _request_headers(type_, api_key, headers)
    base = base_url.rstrip("/")
    try:
        status, body = await _get(client, f"{base}/models", request_headers)
        list_key, name_key = "data", "id"
        if status == 404 and type_ == "openai":
            origin = base[:-3] if base.endswith("/v1") else base
            status, body = await _get(client, f"{origin}/api/tags", request_headers)
            list_key, name_key = "models", "name"
        if status != 200:
            raise DiscoveryError(f"HTTP {status}: {body.decode(errors='replace')}")
        items = json.loads(body).get(list_key) or []
        names = [str(i[name_key]) for i in items if isinstance(i, dict) and i.get(name_key)]
    except DiscoveryError as e:
        raise DiscoveryError(_scrub(str(e), secrets)) from None
    except httpx.TimeoutException:
        raise DiscoveryError(f"Timed out after {TIMEOUT:g} s.") from None
    except httpx.HTTPError as e:
        raise DiscoveryError(_scrub(f"Connection failed: {e}", secrets)) from None
    except (ValueError, AttributeError, TypeError):
        raise DiscoveryError("Unexpected response format; expected an OpenAI-style model list.") from None
    return list(dict.fromkeys(names))[:MAX_MODELS]


async def probe(
    client: httpx.AsyncClient,
    type_: str,
    base_url: str,
    api_key: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict:
    started = time.perf_counter()
    try:
        found = await list_models(client, type_, base_url, api_key, headers)
    except DiscoveryError as e:
        return {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "models_found": 0, "error": str(e)}
    return {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "models_found": len(found), "error": None}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_discovery.py`
Expected: `10 passed`.

- [ ] **Step 5: Commit (ask the user first)**

```bash
git add jev_router/services/discovery.py tests/test_discovery.py
git commit -m "Add model discovery and connection probing for providers"
```

---

### Task 7: Settings API

**Files:**
- Create: `jev_router/api/settings.py`
- Modify: `jev_router/main.py` (include the router)
- Modify: `jev_router/api/chat.py:38-55` (`/v1/models` gains `enabled`)
- Modify: `tests/conftest.py` (`client_host`, GET listings, request capture)
- Test: `tests/test_settings_api.py`

**Interfaces:**
- Consumes: `SettingsService` (Task 5), `list_models`, `probe`, `DiscoveryError` (Task 6), `mask` (Task 1), `_extract_key` and `APIError` from `jev_router/api/deps.py`, `app.state.http`, `app.state.registry`, `app.state.settings`.
- Produces (used by the dashboard in Task 8):
  - `GET /v1/settings` → `{access: "admin_key" | "loopback", secret_source: "env" | "file", warnings: [str]}`
  - `GET /v1/settings/providers` → `{data: [provider], warnings}` where provider = `{name, type, base_url, source, enabled, available, local, key: {required, set, source, hint, env}, enabled_env, headers: {name: "••••"}, model_count, enabled_models}`
  - `POST /v1/settings/providers` (201, provider), `PATCH /v1/settings/providers/{name}` (provider), `DELETE /v1/settings/providers/{name}` (204)
  - `POST /v1/settings/providers/test`, `POST /v1/settings/providers/{name}/test` → `{ok, latency_ms, models_found, error}`
  - `GET /v1/settings/providers/{name}/discover` → `{data: [{model, id, registered, suggested}]}` or `502 discovery_failed`
  - `GET /v1/settings/models` → `{data: [model], warnings}` where model = every `ModelSpec` field plus `source`, `available`, `overridden`
  - `POST /v1/settings/models` (201, `{data: [model]}`), `PATCH /v1/settings/models/{id:path}` (model), `DELETE /v1/settings/models/{id:path}` (204)
  - `GET /v1/settings/export` → `text/yaml`
  - `/v1/models` entries gain `enabled: bool`.
  - Test fixture: `make_client(client_host="127.0.0.1", ...)`; `FakeUpstream.listed: dict[host, dict]` with keys `models` (OpenAI list), `tags` (Ollama list) or `status` + `message`; `FakeUpstream.chat_requests: list[httpx.Request]`.

- [ ] **Step 1: Extend the test fixtures**

In `tests/conftest.py`:

1. In `FakeUpstream.__init__` add:

```python
        self.listed: dict[str, dict[str, Any]] = {}
        self.chat_requests: list[httpx.Request] = []
```

2. Add a method after `_anthropic`:

```python
    def _list(self, request: httpx.Request) -> httpx.Response:
        spec = self.listed.get(request.url.host)
        if spec is None:
            return httpx.Response(404, text="not found")
        if "status" in spec:
            return httpx.Response(spec["status"], text=spec.get("message", ""))
        if request.url.path.endswith("/api/tags") and "tags" in spec:
            return httpx.Response(200, json={"models": [{"name": n} for n in spec["tags"]]})
        if request.url.path.endswith("/models") and "models" in spec:
            return httpx.Response(200, json={"object": "list", "data": [{"id": n} for n in spec["models"]]})
        return httpx.Response(404, text="not found")
```

3. Replace `handler` with:

```python
    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return self._list(request)
        body = json.loads(request.content or b"{}")
        if request.url.host == "api.typesafe.ai":
            return self._jev(body)
        if request.url.path.endswith("/chat/completions"):
            self.chat_requests.append(request)
            return self._chat(body)
        if request.url.path.endswith("/messages"):
            return self._anthropic(body)
        return httpx.Response(404, json={"error": "unexpected url " + str(request.url)})
```

4. In `make_client`, change the factory signature and client construction:

```python
    def factory(client_host: str = "testclient", **overrides: Any) -> TestClient:
```

```python
        client = TestClient(create_app(Settings(**values), transport=upstream.transport), client=(client_host, 50000))
```

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: all existing tests still pass.

- [ ] **Step 2: Write the failing API tests**

Create `tests/test_settings_api.py`:

```python
import json
import logging
import time
from unittest.mock import ANY

import pytest

P = "/v1/settings/providers"
M = "/v1/settings/models"
SMALL = "liquid/lfm3-1.7b"


@pytest.fixture
def admin(make_client):
    return make_client(client_host="127.0.0.1")


def provider(client, name):
    return next(p for p in client.get(P).json()["data"] if p["name"] == name)


def chat(client, model="auto", content="hey, how are you?"):
    return client.post("/v1/chat/completions", json={"model": model, "messages": [{"role": "user", "content": content}]})


def wait_for_trace(client, trace_id, tries=50):
    for _ in range(tries):
        r = client.get(f"/v1/traces/{trace_id}")
        if r.status_code == 200:
            return r.json()
        time.sleep(0.02)
    raise AssertionError(f"trace {trace_id} was never recorded")


def test_access_rules(make_client):
    remote = make_client()
    r = remote.get("/v1/settings")
    assert r.status_code == 403 and r.json()["error"]["code"] == "settings_forbidden"
    assert "JEV_ROUTER_ADMIN_KEY" in r.json()["error"]["message"]
    local = make_client(client_host="127.0.0.1")
    body = local.get("/v1/settings").json()
    assert body["access"] == "loopback" and body["secret_source"] == "env" and body["warnings"] == []
    assert local.get("/v1/settings", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
    assert local.get(P, headers={"Forwarded": "for=1.2.3.4"}).status_code == 403
    keyed = make_client(client_host="127.0.0.1", jev_router_admin_key="admin-secret-1", jev_router_api_keys="router-key-1")
    assert keyed.get("/v1/settings").status_code == 403
    assert keyed.get("/v1/settings", headers={"Authorization": "Bearer wrong"}).status_code == 403
    assert keyed.get("/v1/settings", headers={"Authorization": "Bearer router-key-1"}).status_code == 403
    ok = keyed.get("/v1/settings", headers={"x-api-key": "admin-secret-1"})
    assert ok.status_code == 200 and ok.json()["access"] == "admin_key"


@pytest.mark.parametrize("body,fragment", [
    ({"name": "Bad Name", "base_url": "http://x/v1"}, "name"),
    ({"name": "lab", "base_url": "ftp://x"}, "base_url"),
    ({"name": "lab", "base_url": "http:///v1"}, "base_url"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"Authorization": "Bearer a"}}, "headers"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"bad header": "v"}}, "headers"),
    ({"name": "lab", "base_url": "http://x/v1", "headers": {"X-Big": "v" * 2049}}, "headers"),
    ({"name": "lab", "base_url": "http://x/v1", "surprise": 1}, "surprise"),
])
def test_provider_validation(admin, body, fragment):
    r = admin.post(P, json=body)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"
    assert fragment in r.json()["error"]["message"]


def test_conflicts_unknowns_and_model_validation(admin):
    assert admin.post(P, json={"name": "openai", "base_url": "http://x/v1"}).status_code == 409
    assert admin.patch(P + "/ghost", json={"enabled": True}).status_code == 404
    assert admin.patch(P + "/openai", json={"headers": {"X-A": "b"}}).status_code == 400
    assert admin.patch(P + "/openai", json={"enabled": None}).status_code == 400
    assert admin.patch(P + "/openai", json={}).status_code == 400
    assert admin.get(P + "/ghost/discover").status_code == 404
    assert admin.post(M, json=[{"provider": "liquid", "model": "lfm3-1.7b"}]).status_code == 409
    assert admin.post(M, json=[{"provider": "ghost", "model": "x"}]).status_code == 400
    assert admin.post(M, json=[]).status_code == 400
    assert admin.patch(M + "/ghost/m", json={"speed": 2}).status_code == 404
    for bad in ({"quality": 11}, {"speed": 0}, {"input_cost": -1}, {"context_window": 0},
                {"modalities": ["text", "video"]}, {"provider": "openai"}):
        r = admin.patch(M + "/" + SMALL, json=bad)
        assert r.status_code == 400, bad


def test_builtin_key_and_ollama_enable(admin):
    r = admin.patch(P + "/openai", json={"api_key": "sk-test-openai-000111"})
    assert r.status_code == 200 and r.json()["available"]
    assert r.json()["key"] == {"required": True, "set": True, "source": "settings", "hint": "sk-…0111", "env": "OPENAI_API_KEY"}
    models = {m["id"]: m for m in admin.get("/v1/models").json()["data"]}
    assert models["openai/gpt-5"]["available"] and models["openai/gpt-5"]["enabled"] is True
    r = admin.patch(P + "/openai", json={"api_key": None})
    assert r.json()["key"]["set"] is False and not r.json()["available"]
    assert admin.patch(P + "/ollama", json={"enabled": True}).json()["available"]
    assert admin.get("/health").json()["providers"] == ["liquid", "ollama"]
    assert admin.delete(P + "/ollama").status_code == 204
    assert not provider(admin, "ollama")["available"]


def test_settings_persist_and_survive_a_lost_secret(make_client, tmp_path):
    db = f"sqlite+aiosqlite:///{tmp_path}/shared.db"
    first = make_client(client_host="127.0.0.1", database_url=db)
    assert first.patch(P + "/groq", json={"api_key": "gsk-persisted-key-1234"}).status_code == 200
    again = make_client(client_host="127.0.0.1", database_url=db)
    groq = provider(again, "groq")
    assert groq["key"]["source"] == "settings" and groq["available"]
    lost = make_client(client_host="127.0.0.1", database_url=db, jev_router_secret="a-different-secret")
    groq = provider(lost, "groq")
    assert groq["key"]["source"] == "unreadable" and not groq["available"]
    assert any("groq" in w for w in lost.get("/v1/settings").json()["warnings"])


def test_custom_provider_end_to_end(admin, upstream):
    upstream.listed["lab.example"] = {"models": ["qwen3-8b", "qwen3-32b", "qwen3-8b"]}
    r = admin.post(P, json={"name": "lab", "base_url": "https://lab.example/v1/", "api_key": "lab-key-abcdef123456"})
    assert r.status_code == 201
    lab = r.json()
    assert lab["base_url"] == "https://lab.example/v1" and lab["source"] == "custom" and lab["local"] is False
    assert lab["key"]["hint"] == "lab…3456" and lab["model_count"] == 0
    assert admin.post(P + "/lab/test").json() == {"ok": True, "latency_ms": ANY, "models_found": 2, "error": None}
    found = admin.get(P + "/lab/discover").json()["data"]
    assert [d["id"] for d in found] == ["lab/qwen3-8b", "lab/qwen3-32b"] and not found[0]["registered"]
    assert found[0]["suggested"]["description"] == "qwen3-8b served by lab"
    r = admin.post(M, json=[{"provider": "lab", "model": "qwen3-8b", "quality": 6, "input_cost": 0.1, "output_cost": 0.2},
                            {"provider": "lab", "model": "qwen3-32b", "quality": 8}])
    assert r.status_code == 201 and [m["id"] for m in r.json()["data"]] == ["lab/qwen3-8b", "lab/qwen3-32b"]
    assert r.json()["data"][0]["source"] == "custom" and r.json()["data"][0]["available"]
    assert admin.get(P + "/lab/discover").json()["data"][0]["registered"]
    models = {m["id"]: m for m in admin.get("/v1/models").json()["data"]}
    assert models["lab/qwen3-8b"]["available"] and models["lab/qwen3-8b"]["owned_by"] == "lab"
    r = chat(admin, model="lab/qwen3-8b")
    assert r.status_code == 200 and r.json()["model"] == "lab/qwen3-8b"
    sent = upstream.chat_requests[-1]
    assert sent.url.host == "lab.example" and sent.headers["authorization"] == "Bearer lab-key-abcdef123456"
    assert json.loads(sent.content)["model"] == "qwen3-8b"
    assert admin.delete(P + "/lab").status_code == 204
    ids = {m["id"] for m in admin.get("/v1/models").json()["data"]}
    assert not {"lab/qwen3-8b", "lab/qwen3-32b"} & ids


def test_model_patch_clears_decision_cache_and_prunes(make_client, upstream):
    c = make_client(client_host="127.0.0.1", decision_cache_ttl=60)
    upstream.route_choice, upstream.complexity, upstream.premium, upstream.task = SMALL, 0.2, 0.02, "chat"
    assert chat(c).status_code == 200
    assert c.get("/health").json()["decision_cache"]["size"] >= 1
    r = c.patch(M + "/" + SMALL, json={"speed": 7})
    assert r.status_code == 200 and r.json()["overridden"] == ["speed"] and r.json()["speed"] == 7
    assert c.get("/health").json()["decision_cache"]["size"] == 0
    assert c.patch(M + "/" + SMALL, json={"speed": 9}).json()["overridden"] == []
    c.patch(M + "/" + SMALL, json={"enabled": False})
    assert next(m for m in c.get("/v1/models").json()["data"] if m["id"] == SMALL)["enabled"] is False
    assert c.delete(M + "/" + SMALL).status_code == 204
    assert next(m for m in c.get(M).json()["data"] if m["id"] == SMALL)["enabled"] is True


def test_ollama_discovery_falls_back_to_tags(admin, upstream):
    upstream.listed["localhost"] = {"tags": ["llama3.2:3b", "qwen3:8b"]}
    data = admin.get(P + "/ollama/discover").json()["data"]
    assert [(d["model"], d["id"], d["registered"]) for d in data] == [
        ("llama3.2:3b", "ollama/llama3.2:3b", True), ("qwen3:8b", "ollama/qwen3:8b", False)]
    assert admin.post(P + "/test", json={"type": "openai", "base_url": "http://localhost:11434/v1"}).json()["models_found"] == 2


def test_discovery_failures_are_scrubbed(admin, upstream):
    upstream.listed["lab.example"] = {"status": 401, "message": "bad key lab-key-abcdef123456"}
    admin.post(P, json={"name": "lab", "base_url": "https://lab.example/v1", "api_key": "lab-key-abcdef123456"})
    r = admin.get(P + "/lab/discover")
    assert r.status_code == 502 and r.json()["error"]["code"] == "discovery_failed"
    assert "HTTP 401" in r.text and "lab-key-abcdef123456" not in r.text
    t = admin.post(P + "/lab/test", json={"api_key": "lab-key-abcdef123456"}).json()
    assert t["ok"] is False and "lab-key-abcdef123456" not in t["error"]
    unsaved = admin.post(P + "/test", json={"type": "openai", "base_url": "https://nowhere.example/v1"}).json()
    assert unsaved["ok"] is False and "HTTP 404" in unsaved["error"]


def test_secrets_never_leave_the_server(admin, upstream, caplog):
    caplog.set_level(logging.INFO)
    key, header = "sk-leakcheck-key-998877", "hdr-leakcheck-value-5566"
    assert admin.post(P, json={"name": "lab", "base_url": "https://lab.example/v1", "api_key": key,
                               "headers": {"X-Team": header}}).json()["headers"] == {"X-Team": "••••"}
    admin.post(M, json=[{"provider": "lab", "model": "m1"}])
    admin.patch(P + "/openai", json={"api_key": key})
    r = chat(admin, model="lab/m1")
    assert r.status_code == 200 and upstream.chat_requests[-1].headers["x-team"] == header
    trace = wait_for_trace(admin, r.headers["x-jev-router-trace-id"])
    export = admin.get("/v1/settings/export")
    assert export.status_code == 200 and export.headers["content-type"].startswith("text/yaml")
    assert "lab/m1" in export.text and "LAB_API_KEY" in export.text
    bodies = [admin.get(u).text for u in ("/v1/settings", P, M, "/v1/models", "/health")]
    bodies += [export.text, json.dumps(trace), caplog.text]
    for b in bodies:
        assert key not in b and header not in b
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_settings_api.py`
Expected: FAIL — `/v1/settings` returns `404` (`http_error`), so e.g. `test_access_rules` fails on `assert 404 == 403`.

- [ ] **Step 4: Implement `jev_router/api/settings.py`**

```python
import hmac
import ipaddress
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..crypto import mask
from ..registry import ModelSpec, ProviderSpec
from ..services.discovery import DiscoveryError, list_models, probe
from ..services.settings_service import SettingsService
from .deps import APIError, _extract_key

LOOPBACK = {"127.0.0.1", "::1"}
NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,31}$"
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
NULLABLE_MODEL_FIELDS = {"max_output_tokens"}


async def require_admin(request: Request) -> None:
    admin_key = request.app.state.settings.jev_router_admin_key
    if admin_key:
        supplied = _extract_key(request) or ""
        if hmac.compare_digest(supplied.encode(), admin_key.encode()):
            return
        raise APIError(403, "A valid admin key is required for Settings.", "settings_forbidden", "permission_error")
    host = request.client.host if request.client else ""
    proxied = "x-forwarded-for" in request.headers or "forwarded" in request.headers
    if host in LOOPBACK and not proxied:
        return
    raise APIError(403, "Settings can only be managed from this machine. Set JEV_ROUTER_ADMIN_KEY to manage remotely.",
                   "settings_forbidden", "permission_error")


router = APIRouter(prefix="/v1/settings", tags=["settings"], dependencies=[Depends(require_admin)])


def clean_url(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("must be an http or https URL with a host")
    return value.rstrip("/")


def clean_headers(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if len(value) > 20:
        raise ValueError("at most 20 headers are allowed")
    for name, v in value.items():
        if not HEADER_NAME.match(name):
            raise ValueError(f"invalid header name '{name[:64]}'")
        if name.lower() == "authorization":
            raise ValueError("set api_key instead of an Authorization header")
        if len(v) > 2048:
            raise ValueError(f"header '{name}' is longer than 2048 characters")
    return value


def is_local_url(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    if host in ("localhost", "host.docker.internal"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


Secret = Annotated[str, Field(min_length=1, max_length=4096)]
Modality = Literal["text", "image", "audio"]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("base_url", check_fields=False)
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return v if v is None else clean_url(v)

    @field_validator("headers", check_fields=False)
    @classmethod
    def _headers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        return clean_headers(v)


class ProviderCreate(Body):
    name: str = Field(pattern=NAME_PATTERN)
    type: Literal["openai", "anthropic"] = "openai"
    base_url: str
    api_key: Secret | None = None
    headers: dict[str, str] | None = None
    local: bool | None = None
    enabled: bool = True


class ProviderPatch(Body):
    type: Literal["openai", "anthropic"] | None = None
    base_url: str | None = None
    api_key: Secret | None = None
    headers: dict[str, str] | None = None
    local: bool | None = None
    enabled: bool | None = None


class ProviderProbe(Body):
    type: Literal["openai", "anthropic"] = "openai"
    base_url: str
    api_key: Secret | None = None
    headers: dict[str, str] | None = None


class SavedProviderProbe(Body):
    base_url: str | None = None
    api_key: Secret | None = None


class ModelFields(Body):
    description: Annotated[str, Field(min_length=1, max_length=1000)] | None = None
    strengths: Annotated[list[Annotated[str, Field(max_length=60)]], Field(max_length=20)] | None = None
    quality: Annotated[float, Field(ge=1, le=10)] | None = None
    speed: Annotated[float, Field(ge=1, le=10)] | None = None
    input_cost: Annotated[float, Field(ge=0)] | None = None
    output_cost: Annotated[float, Field(ge=0)] | None = None
    context_window: Annotated[int, Field(gt=0)] | None = None
    max_output_tokens: Annotated[int, Field(gt=0)] | None = None
    modalities: list[Modality] | None = None
    only_when: list[Literal["image", "audio"]] | None = None
    supports_tools: bool | None = None
    supports_json: bool | None = None
    reasoning: bool | None = None
    enabled: bool | None = None


class ModelCreate(ModelFields):
    provider: str = Field(pattern=NAME_PATTERN)
    model: str = Field(min_length=1, max_length=160, pattern=r"^\S+$")


def _svc(request: Request) -> SettingsService:
    return request.app.state.settings_service


def _provider_or_404(request: Request, name: str) -> ProviderSpec:
    p = request.app.state.registry.providers.get(name)
    if p is None:
        raise APIError(404, f"Provider '{name}' not found.", "not_found")
    return p


def _model_or_404(request: Request, model_id: str) -> ModelSpec:
    m = next((m for m in request.app.state.registry.models if m.id == model_id), None)
    if m is None:
        raise APIError(404, f"Model '{model_id}' not found.", "not_found")
    return m


def _reject_nulls(changes: dict[str, Any], allowed: set[str]) -> None:
    if not changes:
        raise APIError(400, "No changes supplied.", "invalid_request")
    nulls = sorted(k for k, v in changes.items() if v is None and k not in allowed)
    if nulls:
        raise APIError(400, f"{nulls[0]}: may not be null", "invalid_request")


def provider_view(request: Request, p: ProviderSpec) -> dict[str, Any]:
    models = [m for m in request.app.state.registry.models if m.provider == p.name]
    key = p.api_key
    return {
        "name": p.name, "type": p.type, "base_url": p.base_url, "source": p.source,
        "enabled": p.enabled, "available": p.available, "local": p.local,
        "key": {"required": p.requires_key, "set": bool(key), "source": p.key_status,
                "hint": mask(key) if key else None, "env": p.api_key_env},
        "enabled_env": p.enabled_env,
        "headers": {name: "••••" for name in p.headers},
        "model_count": len(models), "enabled_models": sum(m.enabled for m in models),
    }


def model_view(request: Request, m: ModelSpec) -> dict[str, Any]:
    svc = _svc(request)
    return {**m.model_dump(), "source": "custom" if svc.is_custom_model(m.id) else "builtin",
            "available": request.app.state.registry.is_available(m), "overridden": svc.overridden(m.id)}


@router.get("")
async def settings_overview(request: Request):
    svc = _svc(request)
    return {"access": "admin_key" if request.app.state.settings.jev_router_admin_key else "loopback",
            "secret_source": svc.box.source, "warnings": svc.warnings}


@router.get("/providers")
async def get_providers(request: Request):
    registry = request.app.state.registry
    return {"data": [provider_view(request, p) for p in registry.providers.values()], "warnings": _svc(request).warnings}


@router.post("/providers", status_code=201)
async def create_provider(body: ProviderCreate, request: Request):
    data = body.model_dump()
    if data["local"] is None:
        data["local"] = is_local_url(data["base_url"])
    await _svc(request).add_provider(data)
    return provider_view(request, request.app.state.registry.providers[body.name])


@router.post("/providers/test")
async def probe_unsaved_provider(body: ProviderProbe, request: Request):
    return await probe(request.app.state.http, body.type, body.base_url, body.api_key, body.headers)


@router.patch("/providers/{name}")
async def patch_provider(name: str, body: ProviderPatch, request: Request):
    changes = body.model_dump(exclude_unset=True)
    _reject_nulls(changes, {"api_key", "headers"})
    await _svc(request).update_provider(name, changes)
    return provider_view(request, _provider_or_404(request, name))


@router.delete("/providers/{name}", status_code=204)
async def delete_provider(name: str, request: Request):
    await _svc(request).delete_provider(name)
    return Response(status_code=204)


@router.post("/providers/{name}/test")
async def probe_saved_provider(name: str, request: Request, body: SavedProviderProbe | None = None):
    p = _provider_or_404(request, name)
    body = body or SavedProviderProbe()
    return await probe(request.app.state.http, p.type, body.base_url or p.base_url, body.api_key or p.api_key, p.headers)


@router.get("/providers/{name}/discover")
async def discover(name: str, request: Request):
    p = _provider_or_404(request, name)
    try:
        names = await list_models(request.app.state.http, p.type, p.base_url, p.api_key, p.headers)
    except DiscoveryError as e:
        raise APIError(502, str(e), "discovery_failed", "upstream_error") from None
    registry = request.app.state.registry
    ids = {m.id for m in registry.models}
    upstream_names = {m.model for m in registry.models if m.provider == name}
    return {"data": [
        {"model": n, "id": f"{name}/{n}", "registered": n in upstream_names or f"{name}/{n}" in ids,
         "suggested": SettingsService.model_defaults(name, n)}
        for n in names
    ]}


@router.get("/models")
async def get_models(request: Request):
    registry = request.app.state.registry
    return {"data": [model_view(request, m) for m in registry.models], "warnings": _svc(request).warnings}


@router.post("/models", status_code=201)
async def create_models(body: list[ModelCreate], request: Request):
    if not 1 <= len(body) <= 200:
        raise APIError(400, "Send between 1 and 200 models.", "invalid_request")
    ids = await _svc(request).add_models([item.model_dump(exclude_none=True) for item in body])
    return {"data": [model_view(request, _model_or_404(request, mid)) for mid in ids]}


@router.patch("/models/{model_id:path}")
async def patch_model(model_id: str, body: ModelFields, request: Request):
    changes = body.model_dump(exclude_unset=True)
    _reject_nulls(changes, NULLABLE_MODEL_FIELDS)
    await _svc(request).update_model(model_id, changes)
    return model_view(request, _model_or_404(request, model_id))


@router.delete("/models/{model_id:path}", status_code=204)
async def delete_model(model_id: str, request: Request):
    await _svc(request).delete_model(model_id)
    return Response(status_code=204)


@router.get("/export")
async def export(request: Request):
    svc, registry = _svc(request), request.app.state.registry
    providers: dict[str, dict[str, Any]] = {}
    for name, override in sorted(svc.overlay.provider_overrides.items()):
        p = registry.providers.get(name)
        if p and override.base_url:
            providers[name] = p.model_dump(
                include={"type", "base_url", "api_key_env", "requires_key", "enabled_env", "local"}, exclude_defaults=True,
            ) | {"base_url": p.base_url}
    for name in sorted(svc.overlay.custom_providers):
        p = registry.providers.get(name)
        if p is None or p.source != "custom":
            continue
        entry: dict[str, Any] = {"type": p.type, "base_url": p.base_url}
        if p.stored_key or p.stored_key_unreadable:
            entry["api_key_env"] = f"{name.upper().replace('-', '_')}_API_KEY"
        else:
            entry["requires_key"] = False
        if p.local:
            entry["local"] = True
        providers[name] = entry
    wanted = set(svc.overlay.custom_models) | set(svc.overlay.model_overrides)
    models = [m.model_dump(exclude_defaults=True) for m in registry.models if m.id in wanted]
    text = ("# Jev Router settings export. Merge into config/models.yaml.\n"
            "# Keys and header values are not exported: set each api_key_env in .env.\n"
            + yaml.safe_dump({"providers": providers, "models": models}, sort_keys=False, allow_unicode=True))
    return PlainTextResponse(text, media_type="text/yaml")
```

Notes for the implementer:
- `/providers/test` is declared before `/providers/{name}` routes; it is a `POST` with no further path segment, so it never collides with `PATCH/DELETE /providers/{name}` or `POST /providers/{name}/test`.
- `model_dump(exclude_defaults=True)` keeps `id`, `provider`, `model`, `description` (they have no defaults).

- [ ] **Step 5: Include the router and expose `enabled` on `/v1/models`**

In `jev_router/main.py`, change the import to `from .api import chat, observability, routing, settings as settings_api` and add after `app.include_router(observability.router)`:

```python
    app.include_router(settings_api.router)
```

In `jev_router/api/chat.py`, inside the per-model dict, after `"available": registry.is_available(m),` add:

```python
            "enabled": m.enabled,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_settings_api.py`
Expected: `16 passed`.

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: all pass.

- [ ] **Step 7: Commit (ask the user first)**

```bash
git add jev_router/api/settings.py jev_router/api/chat.py jev_router/main.py tests/conftest.py tests/test_settings_api.py
git commit -m "Add the /v1/settings API for providers, keys and models"
```

---

### Task 8: Dashboard Settings page

**Files:**
- Modify: `jev_router/dashboard/index.html` (CSS, nav, new section, JS)
- Test: `tests/test_api.py` (dashboard markup check)

**Interfaces:**
- Consumes: every endpoint listed under Task 7 "Produces"; `/v1/models` `enabled`; existing dashboard helpers `$`, `$$`, `esc`, `toast`, `copy`, `empty`, `sk`, `n`, `fixed`, `health`, `loadModels`, `closeDrawer`, `show`, the `#drawer` / `#drawerBg` elements and the document-level `data-close` / `data-go` / `data-copy` delegation.
- Produces: `VIEWS.settings`, `#view-settings`, `sapi(path, opts)` (sends `sessionStorage.jevAdminKey` as `Authorization: Bearer`, only to `/v1/settings*`), `loadSettings()`, `openProvider(name | null, openModel?)`, `afterChange()`.

There is no JS test runner in this repo. The pytest check guards the markup; behaviour is verified in the browser (Step 11).

- [ ] **Step 1: Write the failing markup test**

Append to `tests/test_api.py`:

```python
def test_dashboard_has_settings_view(client):
    html = client.get("/").text
    assert 'data-view="settings"' in html and 'id="view-settings"' in html
    assert 'sessionStorage.getItem("jevAdminKey")' in html
    assert "Add a key in" in html and 'data-go="settings"' in html
```

- [ ] **Step 2: Run it to verify it fails**

Run: `.venv/bin/python -m pytest -q -p no:asyncio tests/test_api.py::test_dashboard_has_settings_view`
Expected: FAIL on the first assert.

- [ ] **Step 3: Add the CSS**

In `jev_router/dashboard/index.html`, directly after the line `  .hint-env { font-size: 12.5px; color: var(--warn); }` insert:

```css
  .hint-env a { color: inherit; font-weight: 600; }

  /* settings */
  .prow { display: grid; grid-template-columns: minmax(0, 1fr) 190px 90px 120px; gap: 12px; align-items: center; width: 100%; text-align: left; border: 0; border-bottom: 1px solid var(--line-2); background: none; padding: 12px 8px; cursor: pointer; border-radius: 8px; }
  .prow:last-child { border-bottom: 0; }
  .prow:hover { background: var(--paper); }
  .prow-main { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
  .prow > .badge { justify-self: end; }
  .warn-card { background: var(--warn-soft); border-color: transparent; color: var(--warn); }
  .warn-card ul { margin: 8px 0 0; padding-left: 18px; }
  .fgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .fgrid3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
  .hrow { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.4fr) 28px; gap: 8px; align-items: center; margin-bottom: 8px; }
  .found { max-height: 280px; overflow-y: auto; border: 1px solid var(--line); border-radius: 9px; padding: 4px 12px; background: #fff; }
  .frow { display: flex; align-items: center; gap: 8px; margin: 0; padding: 6px 0; color: var(--ink); font-size: 13px; cursor: pointer; }
  .frow.reg { color: var(--faint); cursor: default; }
  input[type="checkbox"] { width: auto; accent-color: var(--ink); }
  input[type="range"] { padding: 0; border: 0; background: none; accent-color: var(--ink); box-shadow: none !important; }
  .medit { border-bottom: 1px solid var(--line-2); }
  .medit:last-child { border-bottom: 0; }
  .medit > summary { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; padding: 10px 6px; border-radius: 8px; }
  .medit > summary:hover { background: var(--paper); }
  .mbody { padding: 4px 6px 14px; }
  .edited { display: inline-block; width: 6px; height: 6px; border-radius: 50%; background: var(--violet); margin-left: 6px; vertical-align: 2px; }
  .ok-text { color: var(--ok); font-weight: 500; }
  .bad-text { color: var(--bad); }
```

In the `@media (max-width: 860px)` block, after `    h1 { font-size: 26px; }` add:

```css
    .prow { grid-template-columns: minmax(0, 1fr) auto; row-gap: 4px; }
    .prow > :nth-child(2) { grid-column: 1; grid-row: 2; }
    .prow > :nth-child(3) { grid-column: 2; grid-row: 2; }
    .prow > :nth-child(4) { grid-column: 2; grid-row: 1; }
```

In the `@media (max-width: 560px)` block, replace `    nav button { flex: 1; justify-content: center; padding: 6px 8px; }` with:

```css
    nav button { flex: 1; justify-content: center; padding: 6px 2px; font-size: 12.5px; }
    .fgrid, .fgrid3 { grid-template-columns: minmax(0, 1fr); }
```

- [ ] **Step 4: Add the nav item, the section and the Models sub line**

After the Models nav button (`<button data-view="models">…Models</button>`) add:

```html
      <button data-view="settings"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>Settings</button>
```

Replace the Models header line:

```html
      <div class="head"><div><h1>Models</h1><p class="sub">Registry from <span class="mono">config/models.yaml</span>. <span id="mCount"></span></p></div></div>
```

with:

```html
      <div class="head"><div><h1>Models</h1><p class="sub">Registry from <span class="mono">config/models.yaml</span> plus changes made in Settings. <span id="mCount"></span></p></div></div>
```

After the closing `</section>` of `#view-models` (before `</main>`) add:

```html
    <section id="view-settings" class="hidden">
      <div class="head"><div><h1>Settings</h1><p class="sub">Provider keys, custom endpoints and local models. Changes apply without a restart.</p></div>
        <button class="btn primary" id="sAdd">Add provider</button></div>
      <div class="card" id="sAccess"></div>
      <div id="sWarnings"></div>
      <div class="card mt"><div class="card-h"><h2>Providers</h2><span class="muted small" id="sCount"></span></div><div id="sProviders"></div></div>
      <div class="card mt"><div class="card-h"><h2>Export</h2><div class="row"><button class="btn sm" id="sExportCopy">Copy YAML</button><button class="btn sm" id="sExportDl">Download</button></div></div>
        <p class="muted small" style="margin:0">Custom providers, added models and edits in <span class="mono">models.yaml</span> format. Keys and header values are not included.</p></div>
    </section>
```

- [ ] **Step 5: Constants and state**

Replace:

```js
const VIEWS = { overview: "Overview", playground: "Playground", traces: "Traces", models: "Models" };
```

with:

```js
const VIEWS = { overview: "Overview", playground: "Playground", traces: "Traces", models: "Models", settings: "Settings" };
```

After the `PROVIDERS` line add:

```js
const PRESETS = [
  ["Ollama", "ollama", "http://localhost:11434/v1"],
  ["LM Studio", "lmstudio", "http://localhost:1234/v1"],
  ["vLLM", "vllm", "http://localhost:8000/v1"],
  ["llama.cpp", "llamacpp", "http://localhost:8080/v1"],
  ["OpenRouter", "openrouter", "https://openrouter.ai/api/v1"],
  ["Together", "together", "https://api.together.xyz/v1"],
  ["Fireworks", "fireworks", "https://api.fireworks.ai/inference/v1"],
  ["Mistral", "mistral", "https://api.mistral.ai/v1"],
  ["Custom OpenAI-compatible", "", ""],
];
const HEADER_ROW = `<div class="hrow"><input class="hname" placeholder="X-Header" aria-label="Header name" spellcheck="false"><input class="hval" type="password" placeholder="Value" aria-label="Header value" autocomplete="new-password"><button type="button" class="icon-btn" data-hdel aria-label="Remove header">×</button></div>`;
```

In the `state` object literal, replace `loaded: false };` with:

```js
loaded: false, sp: [], sm: [], failed: {}, panelProvider: null, hdrDirty: false, localTouched: false };
```

- [ ] **Step 6: Models page hint and badge**

Directly above `async function loadModels() {` add:

```js
function modelHint(m) {
  if (m.available) return "";
  const go = `<a href="#settings" data-go="settings">Settings</a>`;
  if (m.enabled === false) return `<div class="hint-env">Turned off in ${go}</div>`;
  if (m.requires_key === false) return `<div class="hint-env">Turn the provider on in ${go}${m.enable_env ? ` or set <code>${esc(m.enable_env)}</code>=true` : ""}</div>`;
  return `<div class="hint-env">Add a key in ${go}${m.enable_env ? ` or set <code>${esc(m.enable_env)}</code>` : ""}</div>`;
}
```

In `loadModels`, replace:

```js
`<span class="badge">${m.requires_key === false ? "Disabled" : "No key"}</span>`
```

with:

```js
`<span class="badge">${m.enabled === false || m.requires_key === false ? "Disabled" : "No key"}</span>`
```

and replace the whole line

```js
          ${!m.available && m.enable_env ? `<div class="hint-env">Set <code>${esc(m.enable_env)}</code>${m.requires_key === false ? "=true" : ""} to enable</div>` : ""}
```

with:

```js
          ${modelHint(m)}
```

- [ ] **Step 7: Overview button, health refresh, drawer label and focus**

In `renderQuickstart`, replace:

```js
      <div class="row wrap mt"><button class="btn lime" data-go="playground">Try it in Playground</button><a class="btn" href="/docs" target="_blank" rel="noopener">API docs</a></div></div>
```

with:

```js
      <div class="row wrap mt"><button class="btn lime" data-go="playground">Try it in Playground</button>${state.health && !state.health.available_models.length ? `<button class="btn" data-go="settings">Add a provider</button>` : ""}<a class="btn" href="/docs" target="_blank" rel="noopener">API docs</a></div></div>
```

In `health()`, after `    state.health = h;` add:

```js
    if (state.view === "overview" && !$("#ovEmpty").classList.contains("hidden")) renderQuickstart();
```

In `openTrace`, after `  state.drawerId = id;` add:

```js
  d.setAttribute("aria-label", "Trace details");
```

In `closeDrawer`, replace:

```js
  const back = state.lastFocus?.isConnected ? state.lastFocus : $(`#traceTable tr[data-id="${CSS.escape(state.drawerId || "")}"]`);
```

with:

```js
  const back = state.lastFocus?.isConnected ? state.lastFocus
    : state.view === "settings" ? $(`#sProviders [data-provider="${CSS.escape(state.panelProvider || "")}"]`)
    : $(`#traceTable tr[data-id="${CSS.escape(state.drawerId || "")}"]`);
```

- [ ] **Step 8: Settings logic**

Directly above the `/* shell */` comment insert:

```js
/* settings */
async function sapi(path, { text: asText = false, ...opts } = {}) {
  const key = sessionStorage.getItem("jevAdminKey");
  const res = await fetch("/v1/settings" + path, { ...opts, headers: { "Content-Type": "application/json", ...(key ? { Authorization: "Bearer " + key } : {}) } });
  const text = await res.text();
  if (!res.ok) {
    let body; try { body = JSON.parse(text); } catch { body = {}; }
    const err = new Error(body?.error?.message || res.statusText || "Request failed");
    err.status = res.status;
    throw err;
  }
  if (asText) return text;
  return text ? JSON.parse(text) : null;
}
const flip = (el) => el.setAttribute("aria-checked", String(el.getAttribute("aria-checked") !== "true"));
const mpath = (id) => "/models/" + id.split("/").map(encodeURIComponent).join("/");
function guessLocal(url) {
  let host;
  try { host = new URL(url.trim()).hostname.replace(/^\[|\]$/g, ""); } catch { return false; }
  return host === "localhost" || host === "host.docker.internal" || host === "[::1]" || /^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(host);
}
function confirmStep(btn, text, action) {
  if (btn.dataset.armed) { action(); return; }
  const old = btn.textContent;
  btn.dataset.armed = "1"; btn.textContent = text;
  setTimeout(() => { if (btn.isConnected) { delete btn.dataset.armed; btn.textContent = old; } }, 4000);
}
const keyText = (p) => ({ settings: `Key saved · ${p.key.hint}`, env: "Key from .env", unreadable: "Key unreadable" }[p.key.source]
  || (p.key.required || p.source === "custom" ? "No key" : "No key needed"));
function keyLine(p) {
  if (!p) return "Stored encrypted. Leave empty if the endpoint needs no key.";
  return { settings: `Saved key ${p.key.hint}`, env: `Using ${p.key.env} from .env (${p.key.hint})`, unreadable: "The saved key can't be decrypted. Enter it again." }[p.key.source]
    || (p.source === "custom" ? "No key saved." : p.key.required ? `No key. Paste one here${p.key.env ? ` or set ${p.key.env}` : ""}.` : "No key needed.");
}
function providerStatus(p) {
  if (!p.enabled) return ["Off", ""];
  if (p.key.source === "unreadable") return ["Key unreadable", "bad"];
  if (p.key.required && !p.key.set) return ["Needs key", "warn"];
  if (state.failed[p.name]) return ["Unreachable", "bad"];
  return ["Ready", "ok"];
}

function renderAccess(info, error) {
  const box = $("#sAccess");
  if (info) {
    const secret = info.secret_source === "env" ? "Keys are encrypted with <code>JEV_ROUTER_SECRET</code>." : "Keys are encrypted with <code>data/secret.key</code>. Back it up with the database.";
    box.innerHTML = info.access === "admin_key"
      ? `<div class="row between wrap"><div><h2>Access</h2><p class="muted small mt-s" style="margin-bottom:0">Admin key accepted. ${secret}</p></div><button class="btn sm" id="sForget">Forget key</button></div>`
      : `<h2>Access</h2><p class="muted small mt-s" style="margin-bottom:0">Managing from this machine. Set <code>JEV_ROUTER_ADMIN_KEY</code> to manage remotely. ${secret}</p>`;
    $("#sForget")?.addEventListener("click", () => { sessionStorage.removeItem("jevAdminKey"); loadSettings(); });
    return;
  }
  box.innerHTML = `<h2>Access</h2><p class="muted small mt-s">${esc(error)}</p>
    <form class="row wrap" id="sKeyForm"><input id="sKey" type="password" placeholder="Admin key" autocomplete="off" aria-label="Admin key" style="flex:1;min-width:200px"><button class="btn primary">Unlock</button></form>`;
  $("#sKeyForm").onsubmit = (e) => {
    e.preventDefault();
    const v = $("#sKey").value.trim();
    if (!v) return;
    sessionStorage.setItem("jevAdminKey", v);
    loadSettings();
  };
}

function renderProviders() {
  const list = [...state.sp].sort((a, b) => (b.available - a.available) || a.name.localeCompare(b.name));
  $("#sCount").textContent = `${state.sp.filter((p) => p.available).length} of ${state.sp.length} available`;
  $("#sProviders").innerHTML = list.map((p) => {
    const [label, cls] = providerStatus(p);
    return `<button class="prow" data-provider="${esc(p.name)}">
      <span class="prow-main"><span class="row wrap"><b>${esc(PROVIDERS[p.name] || p.name)}</b><span class="badge">${esc(p.type)}</span>${p.source === "custom" ? `<span class="badge violet">custom</span>` : ""}${p.local ? `<span class="badge">local</span>` : ""}</span>
        <span class="mono small muted ellip">${esc(p.base_url)}</span></span>
      <span class="small muted">${esc(keyText(p))}</span>
      <span class="small muted num">${p.enabled_models}/${p.model_count} models</span>
      <span class="badge ${cls}">${label}</span></button>`;
  }).join("") || empty("No providers");
}

async function loadSettings() {
  if (!state.sp.length) $("#sProviders").innerHTML = Array.from({ length: 4 }, () => `<div style="padding:12px 8px">${sk("100%", 18)}</div>`).join("");
  try {
    const [info, provs, models] = await Promise.all([sapi(""), sapi("/providers"), sapi("/models")]);
    state.sp = provs.data; state.sm = models.data;
    $("#sAdd").disabled = false;
    renderAccess(info);
    $("#sWarnings").innerHTML = info.warnings.length ? `<div class="card mt warn-card"><h2>Needs attention</h2><ul class="small">${info.warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul></div>` : "";
    renderProviders();
  } catch (e) {
    if (e.status !== 403) { $("#sProviders").innerHTML = `<div class="error">${esc(e.message)}</div>`; return; }
    state.sp = []; state.sm = [];
    $("#sAdd").disabled = true;
    renderAccess(null, e.message);
    $("#sWarnings").innerHTML = ""; $("#sCount").textContent = "";
    $("#sProviders").innerHTML = empty("Unlock Settings to see providers");
  }
}
async function afterChange() { await Promise.all([health(), loadModels(), loadSettings()]); }

function advanced(p) {
  const saved = p ? Object.keys(p.headers) : [];
  return `<details class="mt" ${p && (saved.length || p.type !== "openai") ? "open" : ""}><summary class="disc">Advanced</summary>
    <div class="fgrid mt-s">
      <div><label for="fType">API type</label><select id="fType"><option value="openai">OpenAI-compatible</option><option value="anthropic" ${p?.type === "anthropic" ? "selected" : ""}>Anthropic</option></select></div>
      <div><span class="label-t">Network</span><button type="button" class="switch" id="fLocal" role="switch" aria-checked="${p ? p.local : false}"><span class="knob"></span>Local</button></div>
    </div>
    <div class="mt"><span class="label-t">Extra headers</span>
      ${saved.length ? `<div class="row wrap small" style="margin-bottom:8px"><span class="muted">Saved: <span class="mono">${saved.map(esc).join(", ")}</span>. Rows below replace them.</span><button type="button" class="btn sm" id="fHdrClear">Clear saved headers</button></div>` : ""}
      <div id="fHeaders"></div>
      <button type="button" class="btn sm" id="fHdrAdd">Add header</button>
    </div></details>`;
}

function modelEditor(m, i) {
  const ed = (f) => m.overridden.includes(f) ? `<span class="edited" title="Edited"></span>` : "";
  const id = (f) => `me${i}-${f}`;
  const range = (f, label) => `<div><label for="${id(f)}">${label}${ed(f)} <output class="mono" data-out="${f}">${fixed(m[f], 1)}</output></label><input id="${id(f)}" data-f="${f}" type="range" min="1" max="10" step="0.1" value="${m[f]}"></div>`;
  const num = (f, label, attrs) => `<div><label for="${id(f)}">${label}${ed(f)}</label><input id="${id(f)}" data-f="${f}" type="number" ${attrs} value="${m[f] ?? ""}"></div>`;
  const sw = (t, label, on, f = t) => `<button type="button" class="switch" role="switch" data-t="${t}" aria-checked="${on}"><span class="knob"></span>${label}${ed(f)}</button>`;
  const flags = [
    m.source === "custom" ? `<span class="badge violet">custom</span>` : "",
    m.overridden.length ? `<span class="badge">edited</span>` : "",
    m.enabled ? "" : `<span class="badge">off</span>`,
    !m.local && !m.input_cost && !m.output_cost ? `<span class="badge warn">Cost not set</span>` : "",
  ].join("");
  return `<details class="medit" data-mid="${esc(m.id)}"><summary><span class="mono grow">${esc(m.model)}</span>${flags}</summary>
    <div class="mbody">
      <div><label for="${id("description")}">Description${ed("description")}</label><input id="${id("description")}" data-f="description" value="${esc(m.description)}"></div>
      <div class="mt-s"><label for="${id("strengths")}">Strengths, comma separated${ed("strengths")}</label><input id="${id("strengths")}" data-f="strengths" value="${esc(m.strengths.join(", "))}"></div>
      <div class="fgrid mt-s">${range("quality", "Quality")}${range("speed", "Speed")}</div>
      <div class="fgrid3 mt-s">${num("input_cost", "Input $ / 1M", 'min="0" step="any"')}${num("output_cost", "Output $ / 1M", 'min="0" step="any"')}${num("context_window", "Context window", 'min="1" step="1"')}</div>
      <div class="row wrap mt-s">${sw("reasoning", "Reasoning", m.reasoning)}${sw("supports_tools", "Tools", m.supports_tools)}${sw("vision", "Vision", m.modalities.includes("image"), "modalities")}${sw("enabled", "Enabled", m.enabled)}</div>
      <div class="actions mt-s"><button type="button" class="btn sm primary" data-msave>Save model</button>
        ${m.source === "builtin" && m.overridden.length ? `<button type="button" class="btn sm" data-mreset>Reset</button>` : ""}
        ${m.source === "custom" ? `<button type="button" class="btn sm danger" data-mdel>Delete</button>` : ""}
        <span class="small muted" data-mres role="status"></span></div>
    </div></details>`;
}
function modelChanges(box, m) {
  const val = (f) => $(`[data-f="${f}"]`, box).value;
  const on = (t) => $(`[data-t="${t}"]`, box).getAttribute("aria-checked") === "true";
  const next = {
    description: val("description").trim(),
    strengths: val("strengths").split(",").map((s) => s.trim()).filter(Boolean),
    quality: Number(val("quality")), speed: Number(val("speed")),
    input_cost: Number(val("input_cost")), output_cost: Number(val("output_cost")),
    context_window: Number(val("context_window")),
    reasoning: on("reasoning"), supports_tools: on("supports_tools"), enabled: on("enabled"),
    modalities: on("vision") ? [...new Set([...m.modalities, "image"])] : m.modalities.filter((x) => x !== "image"),
  };
  return Object.fromEntries(Object.entries(next).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(m[k])));
}

function providerPanel(p) {
  const custom = !p || p.source === "custom";
  const savedKey = p && ["settings", "unreadable"].includes(p.key.source);
  const models = p ? state.sm.filter((m) => m.provider === p.name) : [];
  return `<div class="dhead"><div style="min-width:0"><div class="eyebrow">${p ? (custom ? "Custom provider" : "Built-in provider") : "Provider"}</div><h2 class="mt-s" style="font:600 20px var(--serif)">${p ? esc(PROVIDERS[p.name] || p.name) : "Add provider"}</h2></div><button class="btn sm" data-close>Close</button></div>
  <div class="dbody">
    ${p ? "" : `<div class="row wrap" id="fPresets">${PRESETS.map(([label], i) => `<button type="button" class="chip" data-preset="${i}">${esc(label)}</button>`).join("")}</div>`}
    <form class="card ${p ? "" : "mt"}" id="fForm" autocomplete="off">
      <div class="fgrid">
        <div><label for="fName">Name</label><input id="fName" value="${esc(p?.name ?? "")}" ${p ? "readonly" : ""} placeholder="lmstudio" spellcheck="false"></div>
        <div><label for="fUrl">Base URL</label><input id="fUrl" value="${esc(p?.base_url ?? "")}" placeholder="http://localhost:1234/v1" spellcheck="false"></div>
      </div>
      <div class="mt"><label for="fKey">API key</label><input id="fKey" type="password" autocomplete="new-password" placeholder="${p?.key.set ? "Leave empty to keep the current key" : "Optional for local servers"}">
        <div class="row wrap mt-s small"><span class="muted">${esc(keyLine(p))}</span>${savedKey ? `<button type="button" class="btn sm" id="fKeyClear">Remove saved key</button>` : ""}</div></div>
      ${custom ? advanced(p) : ""}
      <div class="row wrap mt"><button type="button" class="switch" id="fEnabled" role="switch" aria-checked="${p ? p.enabled : true}"><span class="knob"></span>Enabled</button></div>
      <div class="actions mt">
        <button class="btn primary" id="fSave">${p ? "Save" : "Add provider"}</button>
        <button type="button" class="btn" id="fTest">Test connection</button>
        <span class="grow"></span>
        ${p && custom ? `<button type="button" class="btn danger" id="fDelete">Delete</button>` : ""}
        ${p && !custom ? `<button type="button" class="btn" id="fReset">Reset to defaults</button>` : ""}
      </div>
      <div class="small mt-s" id="fResult" role="status"></div>
    </form>
    ${p ? `<div class="card mt"><div class="card-h"><h2>Models</h2><button type="button" class="btn sm" id="fFetch">Fetch models</button></div><div id="fFound"></div>
      <div id="fModels">${models.length ? models.map(modelEditor).join("") : empty("No models yet. Fetch them from the provider.")}</div></div>` : ""}
  </div>`;
}

function panel(label, html) {
  const d = $("#drawer");
  if (d.classList.contains("hidden")) state.lastFocus = document.activeElement;
  d.setAttribute("aria-label", label);
  d.innerHTML = html;
  d.classList.remove("hidden"); $("#drawerBg").classList.remove("hidden");
  document.body.style.overflow = "hidden";
  return d;
}
function headerRows() {
  const out = {};
  $$("#fHeaders .hrow").forEach((r) => { const k = $(".hname", r).value.trim(); if (k) out[k] = $(".hval", r).value; });
  return Object.keys(out).length ? out : null;
}
function setResult(html, cls = "") {
  const r = $("#fResult");
  if (!r) return;
  r.className = `small mt-s ${cls}`;
  r.innerHTML = html;
}
async function saveProvider(name, changes, msg) {
  try { await sapi("/providers/" + encodeURIComponent(name), { method: "PATCH", body: JSON.stringify(changes) }); }
  catch (e) { setResult(esc(e.message), "error"); return; }
  toast(msg);
  delete state.failed[name];
  await afterChange();
  openProvider(name);
}
async function removeProvider(name, msg, close) {
  try { await sapi("/providers/" + encodeURIComponent(name), { method: "DELETE" }); }
  catch (e) { setResult(esc(e.message), "error"); return; }
  toast(msg);
  delete state.failed[name];
  await afterChange();
  if (close) closeDrawer(); else openProvider(name);
}
async function saveModel(box, m, provider) {
  const ch = modelChanges(box, m), res = $("[data-mres]", box);
  if (!Object.keys(ch).length) { res.className = "small muted"; res.textContent = "No changes."; return; }
  try { await sapi(mpath(m.id), { method: "PATCH", body: JSON.stringify(ch) }); }
  catch (e) { res.className = "small bad-text"; res.textContent = e.message; return; }
  toast("Model saved");
  await afterChange();
  openProvider(provider, m.id);
}
async function removeModel(m, provider, msg) {
  try { await sapi(mpath(m.id), { method: "DELETE" }); }
  catch (e) { toast(e.message, "bad"); return; }
  toast(msg);
  await afterChange();
  openProvider(provider, msg === "Model reset" ? m.id : null);
}
async function discover(name) {
  const box = $("#fFound");
  box.innerHTML = `<div class="muted small" style="padding:8px 0">Fetching…</div>`;
  let data;
  try { data = (await sapi(`/providers/${encodeURIComponent(name)}/discover`)).data; }
  catch (e) { box.innerHTML = `<div class="error">${esc(e.message)}${name === "ollama" ? "<br>Is Ollama running? <code>ollama serve</code>." : ""}</div>`; return; }
  if (!data.length) { box.innerHTML = empty(name === "ollama" ? "No models found. Run: ollama pull llama3.2" : "No models found."); return; }
  const fresh = data.filter((x) => !x.registered).length;
  box.innerHTML = `<div class="found">${data.map((x) => `<label class="frow ${x.registered ? "reg" : ""}"><input type="checkbox" value="${esc(x.model)}" ${x.registered ? "checked disabled" : ""}><span class="mono">${esc(x.model)}</span>${x.registered ? `<span class="badge">added</span>` : ""}</label>`).join("")}</div>
    <div class="row wrap mt-s" style="margin-bottom:12px"><span class="muted small grow">${fresh} new of ${data.length}</span>${fresh ? `<button type="button" class="btn sm" id="fAll">Select all</button><button type="button" class="btn sm primary" id="fAddSel">Add selected</button>` : ""}</div>`;
  $("#fAll")?.addEventListener("click", () => $$(".found input:not(:disabled)", box).forEach((c) => { c.checked = true; }));
  $("#fAddSel")?.addEventListener("click", async () => {
    const picked = $$(".found input:checked:not(:disabled)", box).map((c) => ({ provider: name, model: c.value }));
    if (!picked.length) { toast("Select at least one model", "warn"); return; }
    try { await sapi("/models", { method: "POST", body: JSON.stringify(picked) }); }
    catch (e) { toast(e.message, "bad"); return; }
    toast(`${picked.length} model${picked.length > 1 ? "s" : ""} added`);
    await afterChange();
    openProvider(name);
  });
}

function openProvider(name, openModel = null) {
  const p = name ? state.sp.find((x) => x.name === name) || null : null;
  const custom = !p || p.source === "custom";
  const first = $("#drawer").classList.contains("hidden");
  state.panelProvider = p?.name || null;
  state.hdrDirty = false;
  state.localTouched = !!p;
  const d = panel(p ? "Provider settings" : "Add provider", providerPanel(p));
  if (first) ($("#fName:not([readonly])", d) || $("[data-close]", d)).focus();
  if (openModel) $(`.medit[data-mid="${CSS.escape(openModel)}"]`, d)?.setAttribute("open", "");
  const on = (sel) => $(sel, d)?.getAttribute("aria-checked") === "true";

  $("#fEnabled", d).onclick = (e) => flip(e.currentTarget);
  if (custom) {
    $("#fLocal", d).onclick = (e) => { state.localTouched = true; flip(e.currentTarget); };
    $("#fUrl", d).oninput = () => { if (!state.localTouched) $("#fLocal", d).setAttribute("aria-checked", String(guessLocal($("#fUrl", d).value))); };
    $("#fHdrAdd", d).onclick = () => { $("#fHeaders", d).insertAdjacentHTML("beforeend", HEADER_ROW); state.hdrDirty = true; $("#fHeaders .hrow:last-child .hname", d).focus(); };
    $("#fHeaders", d).oninput = () => { state.hdrDirty = true; };
    $("#fHeaders", d).onclick = (e) => { const x = e.target.closest("[data-hdel]"); if (x) { x.closest(".hrow").remove(); state.hdrDirty = true; } };
    $("#fHdrClear", d)?.addEventListener("click", () => saveProvider(p.name, { headers: null }, "Saved headers cleared"));
  }
  $$("[data-preset]", d).forEach((b) => b.onclick = () => {
    const [, pname, url] = PRESETS[b.dataset.preset];
    if (pname && state.sp.some((x) => x.name === pname)) { openProvider(pname); return; }
    $("#fName", d).value = pname; $("#fUrl", d).value = url; $("#fUrl", d).oninput();
    $(pname ? "#fKey" : "#fName", d).focus();
  });
  $("#fKeyClear", d)?.addEventListener("click", () => saveProvider(p.name, { api_key: null }, "Saved key removed"));

  $("#fForm", d).onsubmit = async (e) => {
    e.preventDefault();
    const url = $("#fUrl", d).value.trim(), key = $("#fKey", d).value.trim();
    if (!p) {
      const body = { name: $("#fName", d).value.trim(), base_url: url, type: $("#fType", d).value, enabled: on("#fEnabled"), local: on("#fLocal") };
      if (key) body.api_key = key;
      const h = headerRows();
      if (h) body.headers = h;
      try { await sapi("/providers", { method: "POST", body: JSON.stringify(body) }); }
      catch (err) { setResult(esc(err.message), "error"); return; }
      toast("Provider added");
      await afterChange();
      openProvider(body.name);
      return;
    }
    const ch = {};
    if (url !== p.base_url) ch.base_url = url;
    if (key) ch.api_key = key;
    if (on("#fEnabled") !== p.enabled) ch.enabled = on("#fEnabled");
    if (custom) {
      if ($("#fType", d).value !== p.type) ch.type = $("#fType", d).value;
      if (on("#fLocal") !== p.local) ch.local = on("#fLocal");
      const h = headerRows();
      if (state.hdrDirty && h) ch.headers = h;
    }
    if (!Object.keys(ch).length) { setResult("No changes to save.", "muted"); return; }
    saveProvider(p.name, ch, "Saved");
  };

  $("#fTest", d).onclick = async (e) => {
    const btn = e.currentTarget, url = $("#fUrl", d).value.trim(), key = $("#fKey", d).value.trim();
    btn.disabled = true;
    setResult("Testing…", "muted");
    let r;
    try {
      r = p
        ? await sapi(`/providers/${encodeURIComponent(p.name)}/test`, { method: "POST", body: JSON.stringify({ ...(url !== p.base_url ? { base_url: url } : {}), ...(key ? { api_key: key } : {}) }) })
        : await sapi("/providers/test", { method: "POST", body: JSON.stringify({ type: $("#fType", d).value, base_url: url, ...(key ? { api_key: key } : {}), ...(headerRows() ? { headers: headerRows() } : {}) }) });
    } catch (err) { btn.disabled = false; setResult(esc(err.message), "error"); return; }
    btn.disabled = false;
    const ollama = (p?.name || $("#fName", d).value) === "ollama" || url.includes(":11434");
    if (r.ok) setResult(`<span class="ok-text">Connected · ${n(r.models_found)} models · ${Math.round(r.latency_ms)} ms</span>${ollama && !r.models_found ? " · Run <code>ollama pull llama3.2</code>." : ""}`);
    else setResult(`${esc(r.error)}${ollama ? "<br>Is Ollama running? <code>ollama serve</code>. From Docker use <code>http://host.docker.internal:11434/v1</code>." : ""}`, "error");
    if (p) { state.failed[p.name] = !r.ok; renderProviders(); }
  };

  if (p) {
    const count = p.model_count;
    $("#fDelete", d)?.addEventListener("click", (e) => confirmStep(e.currentTarget, count ? `Delete provider and its ${count} models?` : "Delete provider?", () => removeProvider(p.name, "Provider deleted", true)));
    $("#fReset", d)?.addEventListener("click", (e) => confirmStep(e.currentTarget, "Remove saved key and URL?", () => removeProvider(p.name, "Reset to defaults", false)));
    $("#fFetch", d).onclick = () => discover(p.name);
    const mbox = $("#fModels", d);
    mbox.oninput = (e) => {
      const r = e.target.closest('input[type="range"]');
      if (r) $(`[data-out="${r.dataset.f}"]`, r.closest(".medit")).textContent = fixed(Number(r.value), 1);
    };
    mbox.onclick = (e) => {
      const box = e.target.closest(".medit");
      if (!box) return;
      const m = state.sm.find((x) => x.id === box.dataset.mid);
      const t = e.target.closest("[data-t]");
      if (t) { flip(t); return; }
      if (e.target.closest("[data-msave]")) { saveModel(box, m, p.name); return; }
      if (e.target.closest("[data-mreset]")) { removeModel(m, p.name, "Model reset"); return; }
      const del = e.target.closest("[data-mdel]");
      if (del) confirmStep(del, "Delete model?", () => removeModel(m, p.name, "Model deleted"));
    };
  }
}
async function exportYaml() { return sapi("/export", { text: true }); }
```

- [ ] **Step 9: Route the view and wire the page**

In `show()`, replace:

```js
  ({ overview: loadOverview, traces: loadTraces, models: loadModels, playground: loadModels })[view]();
```

with:

```js
  ({ overview: loadOverview, traces: loadTraces, models: loadModels, playground: loadModels, settings: loadSettings })[view]();
```

After the line `$("#traceTable").addEventListener("keydown", …);` add:

```js
$("#sAdd").onclick = () => openProvider(null);
$("#sProviders").addEventListener("click", (e) => { const b = e.target.closest("[data-provider]"); if (b) openProvider(b.dataset.provider); });
$("#sExportCopy").onclick = async () => { try { copy(await exportYaml()); } catch (e) { toast(e.message, "bad"); } };
$("#sExportDl").onclick = async () => {
  try {
    const url = URL.createObjectURL(new Blob([await exportYaml()], { type: "text/yaml" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: "jev-router-settings.yaml" });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) { toast(e.message, "bad"); }
};
```

- [ ] **Step 10: Run the tests**

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: all pass (91 tests: 41 existing + 5 + 6 + 6 + 2 + 4 + 10 + 16 + 1).

- [ ] **Step 11: Verify in the browser**

`:8787` may already be served by an earlier run of this app. Check with `lsof -nP -iTCP:8787 -sTCP:LISTEN`; if the process is this project's `jev-router`/uvicorn, stop it. Never touch `:8000`.

Start a throwaway instance (its own DB and key file, so the real `data/` is untouched):

```bash
mkdir -p /tmp/jr-settings
DATABASE_URL=sqlite+aiosqlite:////tmp/jr-settings/jev.db JEV_ROUTER_SECRET_FILE=/tmp/jr-settings/secret.key \
  .venv/bin/jev-router serve --host 127.0.0.1 --port 8787
```

(run it in the background). Open `http://127.0.0.1:8787/#settings` with the Playwright MCP browser at 1440×900, then repeat the layout checks at 390×844. Check each item and fix anything that fails before moving on:

1. Sidebar shows five items; Settings is selected. Access card reads "Managing from this machine…" and names `data/secret.key`.
2. Providers list: Liquid **Ready** · "Key from .env"; OpenAI **Needs key**; Ollama **Off**.
3. Open Ollama. **Test connection** → "Connected · N models · X ms" (local Ollama has `laya:latest`, `qwen2.5:0.5b`). Toggle **Enabled**, **Save** → toast, row shows **Ready**, sidebar model count goes up.
4. **Fetch models** → `llama3.2:3b` and `qwen2.5-coder:7b` are not installed locally, so they are absent; installed models are listed and selectable. Tick `qwen2.5:0.5b`, **Add selected** → it appears under Models with a `custom` badge (no "Cost not set" badge, Ollama is local). Playground model select lists `ollama/qwen2.5:0.5b`; a Send with that model returns an answer.
5. Expand `llama3.2:3b` (built-in), move Quality, **Save model** → `edited` badge and violet dot, **Reset** appears; **Reset** removes both.
6. Expand the added model, **Delete** → label changes to "Delete model?", second click removes it.
7. OpenAI: paste `sk-test-not-a-real-key-0000`, **Save** → "Key saved · sk-…0000", status **Ready**; **Remove saved key** → **Needs key**. **Reset to defaults** asks for a second click.
8. **Add provider** → LM Studio chip fills `lmstudio` and `http://localhost:1234/v1`, Advanced → Local is on. **Test connection** shows a connection error. **Add provider** → row with `custom` and `local` badges. Open it, **Delete** twice → gone. Do not test the vLLM preset (it points at `:8000`, which belongs to another project).
9. Ollama chip in the Add drawer opens the existing Ollama drawer instead of a new form.
10. **Copy YAML** → toast "Copied"; **Download** saves `jev-router-settings.yaml`; neither contains `sk-test`.
11. Models page: OpenAI cards read "Add a key in Settings or set `OPENAI_API_KEY`"; the Settings link opens Settings.
12. Escape closes the drawer and focus returns to the provider row.
13. At 390 px: nav fits all five labels without horizontal scrolling, provider rows wrap to two lines, drawer form fields stack, model editor grids stack.
14. `list_console_messages` shows no errors.

Finish: stop the throwaway server and `rm -rf /tmp/jr-settings`.

- [ ] **Step 12: Commit (ask the user first)**

```bash
git add jev_router/dashboard/index.html tests/test_api.py
git commit -m "Add the dashboard Settings page for providers, keys and models"
```

---

### Task 9: Docs (README and `.env.example`)

**Files:**
- Modify: `README.md` (Docker, Dashboard, new Settings section, API reference, Environment, Add a model or provider, Development, Security)
- Modify: `.env.example` (`# --- Router` section)

**Interfaces:**
- Consumes: env names from Task 1 (`JEV_ROUTER_ADMIN_KEY`, `JEV_ROUTER_SECRET`, `JEV_ROUTER_SECRET_FILE`), routes from Task 7, page and preset names from Task 8.
- Produces: nothing used by code.

- [ ] **Step 1: Docker note**

In `README.md`, replace the paragraph under `### Docker`:

```markdown
Traces persist in `./data`. `./config` is mounted read-only, so you can edit the model registry and restart without rebuilding.
```

with:

```markdown
Traces, Settings and `data/secret.key` persist in `./data`. `./config` is mounted read-only, so you can edit the model registry and restart without rebuilding.

Inside Docker the dashboard is not on loopback, so set `JEV_ROUTER_ADMIN_KEY` in `.env` to use Settings. To reach Ollama on the host, use `http://host.docker.internal:11434/v1` as the Ollama base URL.
```

- [ ] **Step 2: Dashboard section**

Replace `Open \`http://localhost:8000/\`. It has four pages:` with `Open \`http://localhost:8000/\`. It has five pages:`.

Replace the Models bullet:

```markdown
- **Models.** The registry grouped by provider, with the env var that enables each one.
```

with:

```markdown
- **Models.** The registry grouped by provider, including changes made in Settings, with what each disabled model needs.
- **Settings.** Add provider keys, connect OpenAI-compatible servers and local Ollama, discover models and tune how Jev sees them. See [Settings](#settings).
```

- [ ] **Step 3: New Settings section**

Insert this section directly above `## API reference`:

````markdown
## Settings

Settings lets you run Jev Router with your own keys and your own models, without editing YAML or restarting.

- **Bring your own key.** Paste a key for any built-in provider (OpenAI, Anthropic, Gemini, DeepSeek, Groq, OpenRouter, Liquid). A saved key takes precedence over the one in `.env`. Remove it to fall back to `.env`.
- **Any OpenAI-compatible server.** Add a provider with a name and base URL. Presets: Ollama, LM Studio, vLLM, llama.cpp, OpenRouter, Together, Fireworks, Mistral, or Custom. Optional extra headers and a Local flag, which the `privacy` strategy and `privacy: local_only` respect. Local is on by default for `localhost`, private network addresses and `host.docker.internal`.
- **Discover models.** **Fetch models** lists what the server offers (`/models`, or Ollama's `/api/tags`). Pick the ones to add.
- **Tune routing.** Per model: description and strengths (Jev reads these), quality, speed, cost per 1M tokens, context window, reasoning, tools, vision, on/off. Built-in models can be edited too; **Reset** returns them to `config/models.yaml`.
- **Live.** Changes apply to the next request and clear the decision cache. In-flight requests finish on the model they started with.
- **Export.** **Export YAML** gives your custom providers, models and overrides in `models.yaml` shape, without secrets, so you can commit them to config.

Local models with Ollama:

```bash
ollama serve
ollama pull qwen2.5:0.5b
```

Then open Settings → Ollama → **Enabled** → **Test connection** → **Fetch models** → **Add selected**.

Access:

- Without `JEV_ROUTER_ADMIN_KEY`, Settings only answers requests from the same machine (`127.0.0.1` or `::1`, with no proxy headers).
- With `JEV_ROUTER_ADMIN_KEY` set, every Settings call needs it as `Authorization: Bearer <admin key>`. The dashboard asks for it once per browser session.
- Router keys (`JEV_ROUTER_API_KEYS`) never grant Settings access.
````

- [ ] **Step 4: API reference rows**

In the API reference table, insert these rows directly above the `| \`GET\` | \`/health\` |` row:

```markdown
| `GET` | `/v1/settings` | Settings access mode and warnings (admin key or loopback) |
| `GET` `POST` | `/v1/settings/providers` | List providers (keys masked) or add an OpenAI-compatible provider |
| `PATCH` `DELETE` | `/v1/settings/providers/{name}` | Update a provider, or delete a custom one / reset a built-in one |
| `POST` | `/v1/settings/providers/test`, `/v1/settings/providers/{name}/test` | Test a connection with saved or unsaved values |
| `GET` | `/v1/settings/providers/{name}/discover` | Models the provider offers |
| `GET` `POST` | `/v1/settings/models` | List models with overrides, or add models in bulk |
| `PATCH` `DELETE` | `/v1/settings/models/{id}` | Edit routing fields, or delete a custom model / reset a built-in one |
| `GET` | `/v1/settings/export` | Custom providers, models and overrides as YAML, without secrets |
```

Then replace the authentication sentence:

```markdown
Authentication: `Authorization: Bearer <router key>` or `x-api-key: <router key>`.
```

with:

```markdown
Authentication: `Authorization: Bearer <router key>` or `x-api-key: <router key>`. `/v1/settings` uses the admin key instead (see [Settings](#settings)).
```

(Leave the rest of that paragraph, starting with "Rate limits are per key", unchanged.)

- [ ] **Step 5: Environment rows**

In the Environment table, insert after the `JEV_ROUTER_API_KEYS` row:

```markdown
| `JEV_ROUTER_ADMIN_KEY` | | Required for Settings from anywhere but the same machine |
| `JEV_ROUTER_SECRET` | | Master secret that encrypts saved keys. Any string. |
| `JEV_ROUTER_SECRET_FILE` | `data/secret.key` | Used when `JEV_ROUTER_SECRET` is empty. Created on first start with mode `0600`. |
```

- [ ] **Step 6: "Add a model or provider" and Development**

Replace:

```markdown
Set `TOGETHER_API_KEY` and restart. The model is now part of routing.
```

with:

```markdown
Set `TOGETHER_API_KEY` and restart. The model is now part of routing. [Settings](#settings) does the same from the dashboard without a restart, and **Export YAML** turns those changes into this format.
```

In Development, replace `pytest                                  # 41 tests, no network needed (Jev and providers are mocked)` with `pytest                                  # 91 tests, no network needed (Jev and providers are mocked)`.

In the `jev_router/` tree, replace the `services/` line with:

```text
  services/    model router, executor (fallbacks + streaming), guardrails, agent and tool routers, settings, discovery
```

and replace the `api/` line with:

```text
  api/         chat, routing, observability and settings endpoints, auth and rate limiting
```

- [ ] **Step 7: Security bullets**

Replace the first Security bullet:

```markdown
- Provider and Jev keys are read from the environment on the server and are never returned by any endpoint.
```

with:

```markdown
- Provider and Jev keys stay on the server and are never returned by any endpoint. Settings shows only a hint such as `sk-…a1b2`; header values are masked.
- Keys saved in Settings are encrypted at rest (Fernet). The master secret comes from `JEV_ROUTER_SECRET` or `data/secret.key`. Back up `secret.key` together with the database: without it, saved keys can't be read and must be entered again.
- Settings is limited to the same machine unless `JEV_ROUTER_ADMIN_KEY` is set. Set it for Docker and remote hosts.
```

- [ ] **Step 8: `.env.example`**

In `.env.example`, replace:

```bash
JEV_ROUTER_API_KEYS=
# JEV_ROUTER_CONFIG_DIR=config
```

with:

```bash
JEV_ROUTER_API_KEYS=
# Settings access from anywhere but this machine (required in Docker). Router keys don't grant it.
# JEV_ROUTER_ADMIN_KEY=
# Master secret that encrypts keys saved in Settings. Empty = generated into JEV_ROUTER_SECRET_FILE.
# JEV_ROUTER_SECRET=
# JEV_ROUTER_SECRET_FILE=data/secret.key
# JEV_ROUTER_CONFIG_DIR=config
```

- [ ] **Step 9: Verify**

Run: `.venv/bin/python -m pytest -q -p no:asyncio`
Expected: `91 passed`.

Run: `grep -n "four pages\|41 tests" README.md`
Expected: no output.

Run: `grep -n "JEV_ROUTER_ADMIN_KEY" README.md .env.example`
Expected: matches in the Docker note, Settings section, Environment table, Security, and `.env.example`.

Run: `git diff --stat`
Expected: only `README.md` and `.env.example`.

- [ ] **Step 10: Commit (ask the user first)**

```bash
git add README.md .env.example
git commit -m "Document Settings, the admin key and encrypted key storage"
```
