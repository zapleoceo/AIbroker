# Core refactor — design note

Goal: adding or testing a model/provider costs **one registry entry**, not edits
across a dozen files; one attempt template instead of four copies.

## 1. Provider registry (`providers/registry.py`, data in `providers/specs.py`)

`ProviderSpec` (frozen dataclass, one per provider) owns everything that used to
be a scattered table:

| field | replaces |
|---|---|
| `adapter` | `adapters._ADAPTERS` |
| `probe` (`ProbeSpec`) | `health_probes._PROBES` |
| `quota` | `quotas.PROVIDER_QUOTAS` |
| `cooldown_base_s` | `cooldown.COOLDOWN_BASE_S` |
| `json_reliability` (`reliable`/`unreliable`/`incapable`) | `JSON_*_PROVIDERS` |
| `cache_sticky`, `cache_key_param` | `selector._CACHE_STICKY_PROVIDERS` (+ new) |
| `rate_limit_signs`/`auth_signs`/`monthly_signs` | `provider_errors._PROVIDER_*_SIGNS` |
| `max_request_tokens` | `context_limits.SEED_MAX_REQUEST_TOKENS` |
| `max_keys` | `llm_service._MAX_KEYS_BY_PROVIDER` |
| `paid`, `rank` | `chains.PAID_PROVIDERS`, new deterministic pin order |
| `models` (`ModelSpec`), `defaults`, `rotation` | `DEFAULT_MODEL`, `MODEL_ROTATION`, `register_model`, `_WHISPER_USD_PER_MIN` |
| `empty_is_failure`, `refine_transcript` | `provider == "local"` special cases |

The old module-level names are gone (callers use accessor functions / views).
`ModelSpec` carries `transport` (name of the Protocol implementation), the
capabilities it serves and a price: litellm-priced (default), an override, or
explicitly free/local. `chains.py` keeps the capability -> provider *order*
(policy), the registry keeps provider *facts*.

## 2. Transports (split of `litellm_adapter.py`)

`providers/transport.py` defines small Protocols (`ChatTransport`,
`EmbedTransport`, `TranscribeTransport`) and the name -> instance table.
Implementations: `litellm_client.py` (chat/embed/whisper/chat-transcribe),
`local_vision.py`, `local_asr.py`, `gemini_asr.py`. Pure helpers moved out:
`cost.py` (pricing), `prompt_cache.py` (anthropic marks, cache-token parsing,
stable cache-key kwargs). `call_llm`/`embed`/`transcribe` stay as facades that
dispatch through the registry — no `provider == "local"` anywhere.

## 3. Attempt template (`services/attempt.py`)

`run_attempt(Attempt) -> AttemptResult`: reserve -> decrypt -> call -> release
-> (quality gate) -> record_usage -> affinity, with one error path
(model-unavailable / penalize / too-large learning / booking). Chat, embed,
transcribe, decision and vision supply only a `call` closure and an optional
`check` gate. `run_chat` keeps the chain walk; the other runners shrink to loops.

## 4. Catalog and exact pinning (`providers/catalog.py`)

Built from the registry: model id -> capabilities, serving providers, price.
`GET /v1/models` (project-key auth) lists pin-able ids. A pinned `model`
(`provider/name` or a bare canonical name) resolves to an ordered list of
(provider, model): free-tier keys pass first, then paid, inside each pass by
`ProviderSpec.rank` — deterministic, never shuffled, no rotation. Unknown -> 400
with close-match suggestions (validated at submit, so a typo never burns a job).

## 5. Cache affinity for all capabilities (`routing/affinity.py`)

Key `(project_id, workflow or '', capability, pinned or '')` -> value
`(provider, model, key_id)`, TTL `AFFINITY_TTL_S` (default 2h), in
`shared_state` (Redis, fail-open) with an in-process fallback. It *extends* the
existing per-(project, provider) key pin (same module, one TTL): the route pin
chooses provider+model+key, the legacy pin remains the key tie-break. The
affine target is tried first and re-pinned to whatever succeeded. A paid affine
provider is not promoted ahead of a free chain head (free-first stays policy).
A stable `prompt_cache_key` (hash of the affinity key) is sent only to
providers whose docs define it: openai, mistral, cerebras (`prompt_cache_key`),
openrouter (`session_id`).

## 6. Request trace

`usage_log.request_id` (migration 015 + init.sql + model). A contextvar set by
ASGI middleware (uuid4 hex; jobs use `job-<id>` across all queue retries); every
attempt row carries it; every `/v1` response has `X-Request-Id`. The body field
`request_id` (usage_log.id of the winning row) is unchanged.

## Non-goals / compatibility

Public API and routing order unchanged except: pinned-model resolution (4),
affinity ordering (5), and the extra header/column. Incident history moves to
`docs/history/` with one-line rationales left in code.
