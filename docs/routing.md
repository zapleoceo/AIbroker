# Routing, scopes & cost guard

> **2026-06-26**: Cohere retired `command-r` / `command-r-plus` on 2025-09-15.
> Cohere chain now routes through `command-r7b-12-2024` (small/fast) for
> `chat:fast` / `prefilter` / `structured` and `command-a-03-2025` (flagship)
> for `chat:smart` / `chat:code`. Embed model `embed-english-v3.0` unchanged.
>
> **2026-06-26 (later)**: `chat:edit` chain extended from `[gemini, deepseek]`
> to `[gemini, mistral, cohere, deepseek, anthropic]` — 3 free providers
> in front of paid. mistral + cohere DEFAULT_MODEL now have `chat:edit`
> entries. Existing mistral + cohere keys still need the `llm:edit` scope
> added in the dashboard (or via the bulk migration described in
> `dashboard.md`).

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
| `chat:edit` | **gemini → mistral → cohere → deepseek → anthropic** | `llm:edit` | Coach editor (Stepan). 3 free providers + 2 paid fallbacks; all JSON-reliable. cerebras/groq/openrouter skipped (unreliable JSON). |
| `prefilter` | cerebras → groq → gemini → mistral → cohere → openrouter | `llm:chat` | No paid; cheap pre-filter |
| `structured` | cerebras → groq → gemini → mistral → cohere → openrouter → anthropic → openai | `llm:chat` | |
| `vision` | gemini → anthropic → openai | `llm:vision` | Image input required |
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
3. **Otherwise** — the adaptive per-provider backoff below.

Why: a daily-exhausted key used to get the flat 60 s adaptive cooldown,
recover, get picked again, fail again — a retry storm (~290 wasted calls
every 2 minutes on Cerebras) looping until midnight. Now it's parked once
until reset, so the selector skips it entirely and the storm is gone.

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

`adaptive_cooldown(api_key_id, provider)` queries `usage_log` for recent
429s and returns the right `until` timestamp. `services/llm_service.py`
calls it on every rate-limit error. Vending mode honours the client's
`retry_after_s` instead — the client knows its provider best.

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
