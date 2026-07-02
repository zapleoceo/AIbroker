# Routing, scopes & cost guard

> **2026-06-26**: Cohere retired `command-r` / `command-r-plus` on 2025-09-15.
> Cohere chain now routes through `command-r7b-12-2024` (small/fast) for
> `chat:fast` / `prefilter` / `structured` and `command-a-03-2025` (flagship)
> for `chat:smart` / `chat:code`. Embed model `embed-english-v3.0` unchanged.
>
> **2026-07-01**: `chat:edit` narrowed to JSON-reliable providers only —
> `[gemini, deepseek, anthropic]`. mistral / cohere were dropped (and their
> `chat:edit` DEFAULT_MODEL entries removed): when gemini was on cooldown they
> returned Bahasa-drifted and torn JSON that broke Coach. The `llm:edit` scope
> on existing mistral/cohere keys is now inert — harmless, no cleanup needed.
> Supersedes the 2026-06-26 free-breadth extension.
>
> **2026-07-01**: cerebras quota is TOKENS-only (1M/day). The `req/day` axis
> was dropped from `PROVIDER_QUOTAS` and auto-discover no longer ingests
> cerebras' `x-ratelimit-limit-requests-day` header — a single key logged
> 4,866 req against its 2,400 header without a 429, so it was never a hard cap.
>
> **2026-07-01**: cost tracking restored (`cost_per_token`; `completion_cost`
> had silently zeroed all costs since 2026-06-27) and DeepSeek peak/valley
> pricing added (2x in peak UTC hours from mid-July). See **Cost guard**.

## Capability → provider chain + required scope

Source of truth: `src/aibroker/routing/chains.py` (`CAPABILITY_CHAINS`,
`CAPABILITY_SCOPE`). Routes and the selector import from here — never duplicate
these tables. A drift test asserts every capability has a scope and every
provider in a chain has a `DEFAULT_MODEL` entry.

| Capability | Chain (left→right) | Scope | Notes |
|---|---|---|---|
| `chat:fast` | cerebras → groq → gemini → mistral → cohere → **deepseek** → openrouter → anthropic → openai | `llm:chat` | DeepSeek (paid) precedes slow openrouter for backfill. Documented exception. |
| `chat:smart` | cerebras → groq → gemini → mistral → cohere → anthropic → openrouter → openai → deepseek | `llm:chat` | Strict free-first; expensive last |
| `chat:code` | cerebras → groq → openrouter → gemini → mistral → anthropic → deepseek → openai | `llm:chat` | Codestral via mistral when other free chains are dry |
| `chat:edit` | **gemini → deepseek → anthropic** | `llm:edit` | Coach editor (Stepan). JSON-reliable only: gemini (free, thinking disabled) → deepseek → anthropic (paid). mistral/cohere/cerebras/groq/openrouter excluded — malformed JSON breaks Coach. |
| `prefilter` | cerebras → groq → gemini → mistral → cohere → openrouter | `llm:chat` | No paid; cheap pre-filter |
| `translate` | mistral → gemini → cohere → groq | `llm:chat` | Trivial task: SMALL FAST non-reasoning models first (mistral-small / gemini-flash / cohere-r7b, ~0.3-2s). mistral leads — as reliable at "translate, don't answer" as gpt-oss but 40x faster; cohere-r7b is fastest (~300ms) but occasionally answers instead of translating on ambiguous input, so it's a fallback. cerebras/groq gpt-oss is a REASONING model that "thinks" ~16s on one phrase → starved the caller's timeout. Reuses `llm:chat` keys but hits models the chat chains reach last, so it barely competes with live replies. |
| `structured` | groq → gemini → mistral → cohere → openrouter → anthropic → openai | `llm:chat` | cerebras dropped 2026-07-01: HTTP-200 malformed JSON (~4.6k/wk). groq (same base model) stays. |
| `vision` | gemini → openai | `llm:vision` | anthropic dropped 2026-07-01: 400 "Unable to download the file" on Vera's image URLs (~1.4k/wk). Re-add once images are passed as base64. openai is the paid fallback when gemini is RPM-exhausted. |
| `transcription` | groq → openai | `llm:audio` | Whisper: groq whisper-large-v3-turbo (free) → openai whisper-1. `/v1/transcribe` route |
| `embedding` | voyage → cohere | `llm:embed` | voyage primary; cohere fallback (embed-english-v3) |

`chain_for(cap)` raises `ValueError` on an unknown capability; the proxy rejects
unknown capabilities with HTTP 400 via `is_known_capability`. `scope_for(cap)`
returns the scope the **project** must hold and the **key** must carry.

> **Removed providers.** `sambanova`, `nvidia`, `mistral` were in the chains but
> had no `DEFAULT_MODEL`, so `model_for` returned `None` and they were silently
> skipped — the chains lied about their breadth. They're now out. Re-add only
> with (a) a verified `DEFAULT_MODEL`, (b) a health probe, (c) a prod key test.

## Scopes & the reserved lane

A key's `scopes` (JSONB array) gate which capabilities it can serve; the selector
filters `scopes ? :scope`. The project's `allowed_scopes` gate which capabilities
it may call. Both are checked per request against `scope_for(capability)`.

This gives a **reserved lane** without per-project key ownership (keys stay a
shared pool):

- A gemini key scoped only to `["llm:edit"]` serves **only** `chat:edit` — bot
  `llm:chat` traffic can't select it.
- Mark it `is_reserve = TRUE` and it's picked **last** within its group (see
  selector ordering). Shared edit keys (`["llm:chat","llm:edit"]`) serve first;
  the reserve is the safety net, fresh because nothing else touches it.

So Stepan's Coach always finds a working gemini key for JSON, even when the bot
has driven every shared gemini key into cooldown — and we never pinned a key to
a project. Set it up in the dashboard: edit the key, scopes = `llm:edit`, tick
**reserve**.

## Cooldown resolution — provider-signal first (2026-06-29)

When a call rate-limits, `cooldown_until(key_id, provider, error_msg)` picks
the parking duration most-authoritative-first:

1. **Provider retry-after hint** — if the error carries "Please retry in
   24.5s" / "retry after 30s" / "retryDelay: 24s" (Gemini, OpenAI, Google),
   honour it exactly. The provider knows its own window.
2. **Daily-quota exhaustion** — if the error says "tokens per day" /
   "per day" / "daily limit" (Cerebras "Tokens per day limit exceeded")
   and gave no hint, park the key until **UTC midnight** when the daily
   quota resets.
3. **Per-hour request cap** (2026-07-01) — Cerebras "Requests per hour limit
   exceeded" → park to the top of the next UTC hour. Same anti-storm logic as
   the daily tier, one hour scale. See the full list under **Adaptive
   cooldown** below.
4. **Otherwise** — the adaptive per-provider backoff below.

Why: a daily-exhausted key used to get the flat 60 s adaptive cooldown,
recover, get picked again, fail again — a retry storm (~290 wasted calls
every 2 minutes on Cerebras) looping until midnight. Now it's parked once
until reset, so the selector skips it entirely and the storm is gone. The
per-hour tier fixes the same storm at hour scale.

## Adaptive cooldown (2026-06-26)

The 429 cooldown is no longer a flat 5 min. `routing/cooldown.py` exposes:

| Provider | Base cooldown | Why |
|---|---|---|
| `gemini` | 60s | RPM window resets every 60s |
| `mistral` | 10s | 1 RPS, recovers instantly |
| `cohere` | 60s | per-minute trial limit |
| `cerebras`, `groq`, `voyage` | 60s | rolling RPM |
| `deepseek` | 30s | paid, fast quotas |
| `anthropic`, `openai` | 120s | paid, conservative |
| `openrouter` | 300s | `:free` overload can last minutes |
| _(unknown)_ | 300s | safe default |

Exponential backoff: each consecutive 429 on the same key within a 1h
window doubles the wait (60 → 120 → 240 → 480 …) capped at 30 min.
Counter resets when the key has gone 1h without a 429.

`cooldown_until(api_key_id, provider, error_msg)` resolves the `until`
timestamp most-authoritative-first:

1. **retry-after hint** in the message → wait exactly that.
2. **per-day cap** (`tokens per day`, `rpd`, …) → until UTC midnight.
3. **per-hour cap** — `is_hourly_quota_error()` (cerebras `Requests per hour
   limit exceeded`, 2026-07-01) → `next_hour_boundary()`, the top of the next
   UTC hour. Parking 60s just re-hit the wall and climbed the adaptive backoff
   one 429 at a time; the hour boundary parks a meaningful amount on the first
   hit. Self-calibrating off the provider's own message — no hard-coded
   per-hour rate.
4. **otherwise** → adaptive per-provider backoff (table above).

`services/llm_service.py` calls it on every rate-limit error. Vending mode
honours the client's `retry_after_s` instead — the client knows its provider
best.

## Selector — fair, anti-fingerprint ordering

`src/aibroker/routing/selector.py:pick_and_reserve(provider, scope)`

```sql
UPDATE api_keys SET last_used_at = now()
WHERE id = (
    SELECT id FROM api_keys
    WHERE provider = :provider
      AND is_active AND is_alive
      AND scopes ? :scope
      AND (cooldown_until IS NULL OR cooldown_until < now())
      AND (daily_cost_cap_usd IS NULL OR daily_cost_used_usd < daily_cost_cap_usd)
      AND (daily_limit = 0 OR daily_used < daily_limit)
    ORDER BY
      k.is_reserve,
      <saturation_case>,            -- over-quota keys pushed to back
      COALESCE(r.n, 0),              -- recent-error penalty
      random()                       -- pure random rotation within bucket
    LIMIT 1
    FOR UPDATE OF k SKIP LOCKED
)
```

(Plus `LEFT JOIN`s against `recent` (15-min error count), `toks_today`
(today's per-key req + token sum), and a `defaults` VALUES CTE built from
`PROVIDER_QUOTAS` so the saturation check sees the right quota even when
auto-discover hasn't populated `discovered_*_limit` yet.)

Why each column matters:
1. **`is_reserve`** — non-reserve keys first; reserved Coach safety net is last.
2. **saturation case** — a key whose today's tokens/requests ≥ 95% of its cap
   (per-key `discovered_*` or `PROVIDER_QUOTAS` default) gets a `1`; clean
   peers get `0`. Soft sort, not a hard filter: when **every** peer is
   saturated the picker still returns one rather than fail the request.
3. **recent_errors** — a key that 5× 429'd in the last 15 min waits until
   clean keys are exhausted (within the same saturation bucket).
4. **random()** — true random rotation. Replaced the LRU+`daily_used`
   ordering after we caught one workload-class (Coach JSON-heavy edits)
   monopolising the same handful of cerebras keys to token saturation
   while peers sat idle. Random distributes the next pick uniformly across
   eligible peers.

Atomic, race-free across replicas, advances LRU in one go. Postgres-only
(JSONB `?` + SKIP LOCKED + `random()`); exercised by the Postgres CI job.

`mark_cooldown` normalises tz-aware datetimes to naive UTC — `cooldown_until` is
a naive `TIMESTAMP` and asyncpg rejects offset-aware values.

## Cost guard

`src/aibroker/routing/cost_guard.py:check_caps(api_key, project, estimated_cost)`

Three independent daily caps: per-key, per-project (live SUM from `usage_log`),
global (30s-cached SUM vs `GLOBAL_DAILY_CAP_USD`). Free-tier keys with `cost == 0`
skip the check.

**Cost source** (`providers/litellm_adapter.py:estimate_llm_cost`): LiteLLM's
`cost_per_token(model, prompt_tokens, completion_tokens)` pricing map, summed.
Unpriced models return 0 and are logged **once per model** — the guard must
never silently zero costs. (It did: `completion_cost(prompt_tokens=…)` lost that
kwarg in a LiteLLM bump on 2026-06-27, so every cost read 0 and all $-caps went
blind until 2026-07-01. The one-shot warning + a "known model costs > 0" test
now guard the regression.)

**Free-tier keys always bill $0** (`services/llm_service.py:_billed_cost`).
`estimate_llm_cost` prices by MODEL — it has no idea whether the specific key
that served the call is on a free plan. A free cerebras/gemini/mistral key
calling e.g. `gemini-2.5-flash` gets the same nominal per-token price LiteLLM
would quote a paid caller, even though the free plan absorbs it at $0 real
cost. `_billed_cost(key, meta)` zeroes `meta["cost_usd"]` whenever
`key.tier == "free"`, applied once right after each provider call
(`run_chat`/`run_embed`/`run_transcribe`) so every downstream use — `usage_log`,
the dashboard's per-key/per-project spend, `daily_cost_used_usd` — sees the
real (zero) cost. (Regression: restoring real pricing in the fix above made
free-tier keys show non-zero "spend" for the first time — $5.26 accrued
across 51 free keys in a few hours before this landed. One-time prod cleanup:
zeroed `usage_log.cost_usd` and reset `daily_cost_used_usd` /
`monthly_cost_used_usd` / `total_cost_usd` for tier='free' keys.)

**Peak/valley surcharge** (`providers/peak_pricing.py:peak_multiplier`): DeepSeek
charges 2x during peak UTC hours (01:00–04:00 and 06:00–10:00) from mid-July
2026. `estimate_llm_cost` multiplies the base price by that factor, so the
recorded cost and every $-cap reflect the real peak bill — the same daily budget
buys half as many peak deepseek tokens, and per-key/global caps trip 2x faster in
peak, throttling paid deepseek use without any routing change. Dormant until
`DEEPSEEK_PEAK_FROM` (set the confirmed date when DeepSeek announces it). If peak
deepseek volume ever grows material, the next lever is off-peak backfill
(client-side) or a peak-hours chain demotion — not built yet.

## Selection policy — the whole picture (refactored 2026-06-28)

Choosing which key serves a request is **deterministic, self-calibrating
rules** — no ML, no LLM-judge, and no hardcoded number as a *sole* source
of truth. Every provider-specific constant is a **seed** that real
observations override.

### Resolution tiers (highest wins)

| Signal | manual (operator) | learned (observed) | seed (code) |
|---|---|---|---|
| daily req/tok/in/out quota | `manual_*_limit` | `discovered_*` (response headers) | `PROVIDER_QUOTAS` |
| single-request size ceiling | — | `provider_observations` (from 413s) | `SEED_MAX_REQUEST_TOKENS` |
| cooldown duration | — | adaptive exponential backoff | `COOLDOWN_BASE_S` |

A seed that's overridden by reality is a legitimate bootstrap, not a
crutch: it's used only until the provider teaches us the real value.

**Paid keys drop the seed tier entirely** (`quota_for_key`): `PROVIDER_QUOTAS`
are FREE-tier limits, so a `tier='paid'` key keeps only its explicit
manual/discovered axes — a paid gemini key no longer reads as 212% of the 1,500
free RPD. Its dollar budget lives in the separate `daily_cost_cap_usd` column on
the dashboard, not the quota bar.

### Per request

1. **Size filter** — `run_chat` estimates prompt tokens (≈chars/4) and
   drops providers whose effective ceiling (`min(learned, seed)`) can't fit
   it. groq (free TPM ≈8k) won't be offered a 24k Coach prompt. The request
   still reaches a provider that CAN serve it, so **the answer and its
   quality are identical** — only the guaranteed-failing attempts are
   skipped. If every provider is too small, it falls back to the full chain
   (never starves).

2. **Availability filter** (selector SQL `WHERE`) — active, alive, scope
   match, not in cooldown, under hard cost caps.

3. **Saturation soft-skip** (selector `ORDER BY`) — keys ≥95% on any quota
   axis (req/tok/in/out, resolved manual>discovered>seed) sink to the back;
   used only if every peer is also full.

4. **Fair rotation** — `random()` among the healthy bucket so no key is
   burned first while peers idle.

### Self-learning the size ceiling

When a provider rejects a request as too large (`is_too_large_error`:
413 / context length / "request too large"), `run_chat` records the prompt
size into `provider_observations` (min of all rejections) and jumps to the
next provider — no wasted key retries. Next time, any prompt ≥ that size
skips the provider automatically. So if groq's tier changes, the broker
recalibrates from one rejection instead of waiting for someone to edit a
constant. Measured impact at rollout: ~108 guaranteed-failing groq calls/day
on chat:smart eliminated.

**Two guards against learning garbage** (added 2026-06-29 after a real
incident):

1. **Rate-limit ≠ size.** Groq's TPM 429 literally reads *"Request too
   large for model … tokens per minute (TPM)"*. `is_too_large_error`
   checks `_RATE_LIMIT_MARKERS` first and returns `False` if any match —
   a transient rate-limit must never teach a permanent size ceiling.
2. **Floor.** `record_too_large` refuses to store any ceiling below
   `MIN_LEARNABLE_CEILING` (4 000 tokens) — no real model rejects a
   200-token prompt for size. Without this, `LEAST()` had converged the
   learned ceilings of cerebras/groq/gemini down to ~210 tokens, so the
   broker skipped its three best free providers on essentially every
   prompt and dumped all traffic on mistral. Resetting the bogus rows and
   adding the floor restored free-first routing.

## Failure → next key → next provider

The orchestration lives in `services/llm_service.run_chat` (routes stay thin).
For each provider in the chain it tries **up to `_MAX_KEYS_PER_PROVIDER` (5)
keys** before falling through — a direct client loops all of a provider's keys,
and free keys (esp. gemini) 429 constantly, so a single rate-limited key must
not sink the request. The selector hands a fresh LRU key each pick and
`_penalize` cools failed ones, so each retry gets a different key.

Per key:

1. `pick_and_reserve` (None → no more keys for this provider → next provider).
2. `check_caps` (CostGuardError → audit → next provider; caps are project/global).
3. `call_llm`. Exceptions are classified by `classify_provider_error`:
   - rate-limit / 429 → `mark_cooldown` 5 min, **try next key**.
   - 401/403/auth → `mark_dead`, **try next key**.
   - other → log, **try next key**.
4. **JSON quality gate:** when the request asked for JSON (`response_format`
   type `json_object`/`json_schema`) and the body doesn't parse, the response is
   billed but treated as a failure → **try next key**. Deterministic, no LLM judge.
   Paired with gemini's `reasoning_effort=disable` in the adapter, which stops
   2.5's thinking from eating the token budget and truncating the JSON.

Chain exhausted → HTTP 503. The first success returns text + meta, including the
chosen key's **label** (surfaced to clients for their cost/usage chip).
