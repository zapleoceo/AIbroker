# How to add a provider or a model

Since the core refactor (see [design/core-refactor.md](design/core-refactor.md))
every provider *fact* lives in ONE place: a `ProviderSpec` in
`src/aibroker/providers/specs.py` (types and accessors in
`providers/registry.py`). Order/policy — which provider serves which capability
and in what sequence — stays in `routing/chains.py`.

## Add a new model to an existing provider

1. In the provider's `build_provider(...)` entry:
   - make it the lane's default: `defaults={"chat:smart": "gemini/gemini-4-flash", ...}`, or
   - add it to a rotation: `rotation={"chat:fast": (..., "gemini/gemini-4-flash")}`
     (use a rotation only when the provider meters quota per model — Gemini).
2. Price it. `ModelSpec.pricing` is one of:
   - `"litellm"` (default) — litellm's pricing map knows the model. The registry test
     checks this against litellm itself;
   - `"override"` — litellm does not know it: pass `model_meta={id: {"pricing": "override",
     "input_usd_per_mtok": ..., "output_usd_per_mtok": ..., "cache_read_usd_per_mtok": ...}}`;
     it is registered into litellm's map at import, so every cost path and daily cap
     prices it;
   - `"per_minute"` (audio, `usd_per_minute`), `"free"` (no usable meter / free by
     construction), `"local"` (self-hosted).
3. Run `tests/test_registry.py`. A model that is neither priced nor marked fails the suite
   on purpose: an unpriced model silently books $0 and blinds the daily caps.
4. That is all: the model is now in `GET /v1/models`, pin-able by id or by its bare name,
   rotation/pin resolution and the catalog pick it up. Record the *why* (dates, measurements)
   in `docs/history/provider-choices.md`, one line in the code.

A model served by a non-litellm endpoint sets `transport` in its `model_meta`
(`"local_vision"`, `"local_asr"`, `"gemini_asr"`, `"openrouter_decisions"`; see below).

## Add a new provider

1. Check litellm supports it (`litellm.provider_list`) — or plan a raw-HTTP transport.
2. Add `register(build_provider("acme", ...))` to `providers/specs.py`. The fields:

   | field | what it is |
   |---|---|
   | `rank` | unique; the fixed order among providers that serve the same pinned model (free first, then rank) |
   | `defaults` / `rotation` | capability -> model id (the provider prefix is litellm's routing prefix) |
   | `model_meta` | per-model `pricing`, `transport`, extra capabilities |
   | `paid` | billed per token (a paid-tier tail provider) |
   | `quota` | `Quota(req_per_day, tok_per_day, ..., doc=<url>)` — the seed; manual/discovered limits override it |
   | `cooldown_base_s` | base cooldown after a 429 (the provider's reset cadence) |
   | `max_keys` | keys tried before the walk moves on (default 5) |
   | `json_reliability` | `reliable` / `unreliable` (sunk behind reliable ones on JSON requests) / `incapable` (excluded from JSON requests) |
   | `adapter` | a `ProviderAdapter` subclass from `providers/adapters.py`, only when the provider has request quirks |
   | `probe` | `ProbeSpec(url, body, auth=..., needs_account_id=...)` — the cheapest call that proves a key alive |
   | `quota_headers` | `"openai"` / `"anthropic"` when its rate-limit headers can seed the daily quota |
   | `cache_sticky`, `explicit_cache`, `cache_key_param` | prompt-cache behaviour — set `cache_key_param` ONLY if the provider documents a stable-cache-key request parameter |
   | `rate_limit_signs` / `auth_signs` / `monthly_signs` | provider-scoped error substrings (narrow; never global) |
   | `max_request_tokens` | bootstrap size ceiling (the learned ceiling overrides it) |
   | `empty_is_failure`, `refine_transcript` | local-style providers whose empty output is never a real answer |

3. Give it a slot in `routing/chains.py` (`CAPABILITY_CHAINS`) where it belongs, and make
   sure every `(provider, capability)` pair in a chain has a default model
   (`test_registry.py` enforces it).
4. Add a key: `POST /admin/keys` (or the dashboard) with the provider, label and token.
5. Update `docs/providers.md` (the provider's row) and add the dated rationale to
   `docs/history/provider-choices.md`.

No other file needs editing: cooldown, quotas, probes, JSON shaping, key limits, the
scope checkboxes and the model catalog are all views over the registry.

## A provider that is not a litellm provider (raw HTTP)

Implement the Protocol(s) it serves from `providers/transport.py` —
`ChatTransport.chat`, `EmbedTransport.embed`, `TranscribeTransport.transcribe`,
`DecideTransport.decide` — in its own module (see `local_vision.py`, `local_asr.py`,
`gemini_asr.py`), return `(payload, meta)` with at least `model`, `model_served`,
`tokens_in`, `tokens_out`, `cost_usd`, `latency_ms`, raise errors whose text
`classify_provider_error` understands (reclassify transport failures as `TimeoutError`
so the key cools), register the instance in `transport._load()` and point the model's
`transport` at it. If it needs a longer call ceiling, add `call_timeout(capability)`.
The attempt template, cost guard, usage rows, affinity and the catalog then work for it
unchanged.

## Testing a new model/provider without live keys

- `tests/test_registry.py` — consistency (chains, defaults, pricing, transports).
- `tests/test_pin_resolution.py` — pins resolve and walk deterministically.
- Drive `run_chat` with fakes (`pick_and_reserve`, `call_llm`, `reserve_cost`, …) as in
  `tests/test_affinity.py`; every attempt goes through `services/attempt.py`, so the error
  paths are already covered (`tests/test_attempt.py`).
