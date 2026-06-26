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

## Selector

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
    ORDER BY is_reserve, last_used_at NULLS FIRST, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *
```

`ORDER BY is_reserve` first puts non-reserve keys (FALSE) ahead of reserve
(TRUE); then LRU. Atomic, race-free across replicas, advances LRU in one go.
Postgres-only (JSONB `?` + SKIP LOCKED); exercised by the Postgres CI job.

`mark_cooldown` normalises tz-aware datetimes to naive UTC — `cooldown_until` is
a naive `TIMESTAMP` and asyncpg rejects offset-aware values.

## Cost guard

`src/aibroker/routing/cost_guard.py:check_caps(api_key, project, estimated_cost)`

Three independent daily caps: per-key, per-project (live SUM from `usage_log`),
global (30s-cached SUM vs `GLOBAL_DAILY_CAP_USD`). Free-tier keys with `cost == 0`
skip the check.

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
