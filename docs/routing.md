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

## Size-aware provider filter (2026-06-28)

Before walking the chain, `run_chat` estimates the prompt size
(`providers/context_limits.estimate_prompt_tokens`, ≈chars/4) and drops any
provider whose single-request token ceiling can't fit it
(`PROVIDER_MAX_REQUEST_TOKENS`). Today only **groq** has a ceiling (8k —
its free TPM); a 24k-token Coach prompt 413s on groq 100% of the time, so
sending it is a guaranteed wasted call that just delays the request until
the chain falls through to a provider that can serve it.

This is a **pure efficiency win, zero quality change**: the request lands
on exactly the provider it would have reached after the failures, so the
answer is identical — it just skips the wasted attempts. Measured impact:
~108 guaranteed-failing groq calls/day on chat:smart eliminated.

Safety: if *every* provider in a chain gets size-skipped (impossible today
since big-context providers have no ceiling), it falls back to the full
chain so a request is never starved.

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
