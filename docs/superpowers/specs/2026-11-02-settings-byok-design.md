# Settings: bring your own keys, custom providers, local models

Date: 2026-11-02
Status: approved

## Goal

Let the operator of a Jev Router instance manage providers, keys and models from the dashboard instead of editing `.env` and `config/models.yaml`. Any OpenAI-compatible endpoint (Ollama, LM Studio, vLLM, llama.cpp, Together, Fireworks, Mistral, OpenRouter, ...) can be added, its models discovered and tuned for routing, and changes apply without a restart. The router stays fully usable as config-as-code.

## Decisions

| Question | Decision |
| --- | --- |
| Who manages settings | One operator per instance. Settings are global, not per tenant or per request. |
| Key storage | Encrypted in the existing database (Fernet). Write-only through the API. |
| Adding models | Discover from the endpoint, pick which to add, edit routing metadata. |
| Relation to YAML / `.env` | Layered on top. `models.yaml` is the base; the database stores only changes. A key saved in Settings wins over `.env`. |
| Applying changes | Merge YAML + overlay in memory, validate, commit, swap into the live registry under a lock, clear the decision cache. |

Out of scope: per-tenant settings, per-request keys, multi-replica synchronisation (multi-replica deployments keep using YAML and env), a settings lock mode.

## Architecture

### Components

- **`jev_router/crypto.py`**: `SecretBox` wraps Fernet. (Not `secrets.py`, which would shadow the standard library module.)
  - Master secret: `JEV_ROUTER_SECRET` if set (any string; Fernet key = urlsafe base64 of SHA-256 of the string). Otherwise `data/secret.key` is generated on first run with mode `0600` and reused.
  - `encrypt(str) -> str`, `decrypt(str) -> str | None`. `None` means the value cannot be decrypted (wrong or lost secret); callers treat it as `unreadable`, never crash.
  - New dependency: `cryptography`.
- **`jev_router/settings_store.py`**: async CRUD over four new tables in the existing SQLAlchemy metadata (SQLite or Postgres):
  - `provider_overrides(name PK, base_url, enabled, api_key_enc, updated_at)` for built-in providers.
  - `custom_providers(name PK, type, base_url, enabled, local, api_key_enc, headers_enc, updated_at)`.
  - `model_overrides(id PK, fields JSON, updated_at)` for built-in models; `fields` holds only overridden routing fields.
  - `custom_models(id PK, provider, spec JSON, updated_at)`.
  - A save rewrites all four tables in one transaction, so only `updated_at` is kept.
  - `custom_models.provider` may name a custom or a YAML provider, so there is no foreign key. Deleting a custom provider deletes its custom models in the same transaction (service code). Resetting a built-in provider keeps its custom models.
- **`jev_router/overlay.py`**: pure function `merge(base_providers, base_models, overlay, box) -> MergeResult(providers, models, warnings)`. No I/O; fully unit-testable.
- **`Registry`** gains:
  - `ProviderSpec.stored_key: str | None` and `ProviderSpec.key_status: "settings" | "env" | "unreadable" | None`, plus `local: bool = False` and `source: "builtin" | "custom"`.
  - `ProviderSpec.api_key` precedence: `stored_key` -> env var -> `None`.
  - `ProviderSpec.enabled_override: bool | None`. `available` becomes: `False` if `enabled_override is False`; otherwise, if `enabled_override is None` and `enabled_env` is set, the env var must be truthy (current behaviour, so `OLLAMA_ENABLED` keeps working); then a key is required when `requires_key`. Setting `enabled_override = True` lets keyless providers such as Ollama be switched on from the UI.
  - Models served by a `local` provider are treated as `local: true` for filtering and the `privacy` strategy.
  - `Registry.base`: the YAML-loaded providers and models, kept immutable for re-merging.
  - `Registry.apply(providers, models)`: replaces `providers` and `models` in place. All existing components (`ModelRouter`, `Executor`, `PolicyEngine`, evals, API) keep their reference to the same `Registry` instance and see changes on the next call.
- **`jev_router/services/settings_service.py`**: orchestrates a change. Under one `asyncio.Lock`:
  1. Load the current overlay, apply the requested change in memory.
  2. `merge(...)` and validate (unknown providers, duplicate ids, strategy config still valid). On failure raise `422`; nothing is written.
  3. Commit to the database.
  4. `registry.apply(...)`, `decision_engine.cache.clear()`.
  5. Log one line without secrets, e.g. `settings: provider openai updated (api_key, enabled)`.
- **`jev_router/services/discovery.py`**: `test_connection` and `discover_models` using the shared httpx client.
- **`jev_router/api/settings.py`**: FastAPI router for `/v1/settings/*`.
- **Startup** (`main.py` lifespan): load YAML into `Registry.base`, load overlay, merge, apply. Merge warnings are logged and exposed in API responses.
- **Executor**: unchanged. Custom providers use the existing `OpenAICompatProvider` or `AnthropicProvider` by `type`.

### Data flow

```
models.yaml ──┐
              ├─ merge() ─ validate ─ commit ─ Registry.apply() ─ cache.clear()
DB overlay ───┘                                  │
                                                 └─ ModelRouter / Executor read it on the next request
```

Requests already in flight keep the `ModelSpec` and `ProviderSpec` objects they resolved and finish normally.

## Settings API

All paths are under `/v1/settings`. Errors use the existing envelope `{"error": {"message", "type", "code", ...}}`.

### Access

- `JEV_ROUTER_ADMIN_KEY` set: every Settings endpoint requires it as `Authorization: Bearer <key>` or `x-api-key`. Compared with `hmac.compare_digest`.
- Not set: Settings only accepts requests whose client address is loopback (`127.0.0.1`, `::1`) and that carry no `X-Forwarded-For` or `Forwarded` header (a reverse proxy on the same host would otherwise make every request look local). Others get `403 settings_forbidden` with a message to set `JEV_ROUTER_ADMIN_KEY`.
- Router keys (`JEV_ROUTER_API_KEYS`) never grant Settings access.
- Docker and remote hosts need the admin key; the README states this.

### Secrets in responses

Keys and header values are write-only. Providers expose:

```json
"key": {"required": true, "set": true, "source": "settings", "hint": "sk-…a1b2"}
```

`source` is `settings`, `env`, `unreadable` or `null`. `hint` is the first 3 and last 4 characters when the key is at least 12 characters long, otherwise `"…"` plus the last 2. Header values are returned as `"••••"`. `api_key: null` in a PATCH clears the saved key; `.env` (if any) applies again. Secrets never appear in logs, traces or error messages.

### Providers

| Method | Path | Body / behaviour |
| --- | --- | --- |
| GET | `` (i.e. `/v1/settings`) | `{access: "admin_key" \| "loopback", secret_source: "env" \| "file", warnings: [str]}`. Used by the dashboard to render the Access card. |
| GET | `/providers` | `{data: [provider], warnings: [str]}`. Provider: `name, type, base_url, source, enabled, available, local, key, headers (masked), model_count, enabled_models`. |
| POST | `/providers` | Custom provider: `{name, type: "openai" \| "anthropic" = "openai", base_url, api_key?, headers?, local?, enabled = true}`. `201`. `409 conflict` if the name exists. |
| PATCH | `/providers/{name}` | Built-in: `api_key, base_url, enabled`. Custom: also `type, headers, local`. Unknown fields for the provider kind: `400`. |
| DELETE | `/providers/{name}` | Custom: delete it and its models. Built-in: delete its overrides (back to YAML). `204`. |
| POST | `/providers/test` | Probe values that are not saved yet: `{type, base_url, api_key?, headers?}`. Same response as below. |
| POST | `/providers/{name}/test` | Optional body `{api_key?, base_url?}` to test unsaved values. Always `200 {ok, latency_ms, models_found, error}`. |
| GET | `/providers/{name}/discover` | `{data: [{model, id, registered, suggested: {...model defaults}}]}`. Upstream failure: `502 discovery_failed` with a trimmed message. |

Validation:

- `name`: `^[a-z0-9][a-z0-9_-]{0,31}$`.
- `base_url`: `http` or `https` with a host; trailing slash stripped.
- `headers`: at most 20, names match `^[A-Za-z0-9-]{1,64}$`, values up to 2048 characters. `Authorization` is rejected (use `api_key`).
- `local` defaults to true when the host is `localhost`, `127.0.0.0/8`, `::1`, an RFC 1918 address or `host.docker.internal`.

### Models

| Method | Path | Body / behaviour |
| --- | --- | --- |
| GET | `/models` | `{data: [model], warnings}`. Model: all `ModelSpec` routing fields plus `source`, `available`, `overridden: [field]`. |
| POST | `/models` | Bulk add: `[{provider, model, description?, strengths?, quality?, speed?, input_cost?, output_cost?, context_window?, max_output_tokens?, modalities?, reasoning?, supports_tools?, supports_json?, enabled?}]`. Id is `{provider}/{model}`. `201 {data: [model]}`. All-or-nothing. |
| PATCH | `/models/{id:path}` | Built-in: override any routing field or `enabled`. Custom: any field except `provider`, `model`, `id`. |
| DELETE | `/models/{id:path}` | Custom: delete. Built-in: clear overrides. `204`. |
| GET | `/export` | `text/yaml` in `models.yaml` shape containing custom providers (no `api_key`; `api_key_env` left for the operator to fill), custom models and override values. No secrets. |

Editable routing fields: `description, strengths, quality, speed, input_cost, output_cost, context_window, max_output_tokens, modalities, only_when, supports_tools, supports_json, reasoning, enabled`.

Validation: `quality` and `speed` in `[1, 10]`; costs `>= 0`; `context_window > 0`; `modalities` subset of `text, image, audio`; `provider` must exist; duplicate id `409`.

Defaults for added models: `quality 5, speed 5, context_window 32768, input_cost 0, output_cost 0, modalities [text]`, `description "{model} served by {provider}"`. On non-local providers a model whose costs are both `0` shows a "Cost not set" badge in the dashboard, because the `cheapest` strategy would otherwise over-prefer it.

### Discovery and testing

- `GET {base_url}/models` with the provider's auth. OpenAI shape: `data[].id`.
- On `404` from `/models`, try Ollama's native `GET {origin}/api/tags` (`models[].name`), where `origin` is `base_url` without a trailing `/v1`.
- Anthropic type: `GET {base_url}/models` with `x-api-key` and `anthropic-version: 2023-06-01`.
- Timeout 5 s, `follow_redirects=False`, response body capped at 1 MB, at most 500 models returned.
- Error text is trimmed to 300 characters and scrubbed of the key before returning.

### Error codes

`400 invalid_request` (body or field validation, via the existing validation handler), `403 settings_forbidden`, `404 not_found`, `409 conflict`, `422 invalid_settings` (the merged registry would be invalid), `502 discovery_failed`.

## Dashboard

A fifth sidebar item, **Settings** (gear icon), below Models; it joins the mobile tab bar. It reuses the existing cards, badges, segmented control, drawer and toasts.

### Access card

- The admin key is entered on the Settings page, stored in `sessionStorage` under `jevAdminKey`, and sent only on `/v1/settings` calls.
- States: "Managing from this machine. Set `JEV_ROUTER_ADMIN_KEY` to manage remotely." / admin key accepted / key input shown after a `403`.

### Providers list

- One row per provider: name, type badge, `custom` badge when custom, mono `base_url`, status badge (**Ready**, **Needs key**, **Off**, **Unreachable** after a failed test, **Key unreadable**), key source ("Key from .env", "Key saved · sk-…a1b2", "No key"), enabled model count.
- Clicking a row opens the provider drawer.
- **Add provider** opens the drawer with preset chips that prefill name and URL:

| Preset | name | base_url |
| --- | --- | --- |
| Ollama | `ollama` (built-in) | `http://localhost:11434/v1` |
| LM Studio | `lmstudio` | `http://localhost:1234/v1` |
| vLLM | `vllm` | `http://localhost:8000/v1` |
| llama.cpp | `llamacpp` | `http://localhost:8080/v1` |
| OpenRouter | `openrouter` (built-in) | `https://openrouter.ai/api/v1` |
| Together | `together` | `https://api.together.xyz/v1` |
| Fireworks | `fireworks` | `https://api.fireworks.ai/inference/v1` |
| Mistral | `mistral` | `https://api.mistral.ai/v1` |
| Custom OpenAI-compatible | empty | empty |

  Presets that match a built-in provider open that provider's drawer instead of creating a duplicate.

### Provider drawer

- Fields: name (read-only for built-ins and after creation), base URL, API key (password input, always empty; typing replaces the key; "Remove saved key" clears it).
- **Advanced**: header rows, Local toggle, type select (custom only).
- **Test connection** shows the result inline: "Connected · 12 models · 140 ms" or the error.
- Hints:
  - Ollama unreachable: "Is Ollama running? `ollama serve`. From Docker use `http://host.docker.internal:11434/v1`."
  - Ollama has no models: "Run `ollama pull llama3.2`."
- Actions: **Save**, **Enabled** switch. Built-in: **Reset to defaults**. Custom: two-step **Delete** ("Delete provider and its 3 models?" then confirm).
- **Models**:
  - **Fetch models** lists discovered models with checkboxes; already registered ones are greyed out. **Add selected** creates them with defaults.
  - Each model row expands into an editor: description, strengths (comma separated), quality and speed sliders with values, input/output cost per 1M tokens, context window, reasoning / tools / vision toggles, enabled switch. Overridden built-in fields get an "edited" dot; **Reset** clears overrides. Custom models get **Delete**.

### Elsewhere

- Models page stays read-only. The enable hint becomes "Add a key in Settings or set `OPENAI_API_KEY`" with a link to Settings.
- Overview empty state (no available models) gets an **Add a provider** button that opens Settings.
- After any save: refresh the Playground model select and the sidebar status ("Jev configured · N models").
- Bottom of the page: **Export YAML** with Copy and Download, backed by `/export`.

## Failure handling

- Undecryptable stored key: `key.source = "unreadable"`, provider falls back to its env key if any, startup continues, dashboard asks to re-enter it.
- Override for a model no longer in YAML: ignored, reported in `warnings`.
- Custom provider whose name now exists in YAML: skipped, reported in `warnings`.
- Custom model whose provider is missing: skipped, reported in `warnings`.
- Invalid merge on write: `422`, database unchanged, live registry unchanged.
- Discovery and test calls never raise into the request path; test reports `ok: false`, discover returns `502`.

## Testing

TDD with `pytest` (`.venv/bin/python -m pytest -q -p no:asyncio`).

- `SecretBox`: round trip; wrong secret returns `None`; arbitrary-string secret works; generated key file has mode `0600`; raw DB column never contains the plaintext.
- `merge()`: settings key beats env beats none; built-in model overrides; custom providers and models; enabled override for keyless providers; local provider marks models local; orphaned override, name clash and missing provider produce warnings.
- Settings API:
  - Access: admin key set (missing/wrong/right), unset from loopback (allowed), unset from remote (403), router key on Settings (403).
  - Validation for names, URLs, headers, model fields; `409` duplicates; `404` unknowns; `422` leaves database and registry unchanged.
  - Secret leak scan: save a known key and header value, then assert neither appears in any Settings, `/v1/models` or trace response body.
- End to end with `httpx.MockTransport`: add a custom provider, discover, add two models, see them in `/v1/models`, chat routes to one, PATCH a model and see the decision cache cleared, delete the provider and its models disappear.
- Discovery: OpenAI shape, Ollama `/api/tags` fallback after `404`, timeout, `401`, oversized body.
- Manual: live Ollama if installed; Settings page in the browser at 1440 px and 390 px covering add, test, fetch, add models, edit, reset, delete, export.

## Documentation

- README: new **Settings** section (access rules, admin key, presets, export), Docker note on `JEV_ROUTER_ADMIN_KEY` and `host.docker.internal`, `.env.example` gains `JEV_ROUTER_ADMIN_KEY` and `JEV_ROUTER_SECRET`.
- Security section: keys encrypted at rest, master secret location, back up `data/secret.key` with the database.
