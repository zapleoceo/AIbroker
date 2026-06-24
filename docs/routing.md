# Routing & cost guard

## Capability → provider chain

Source of truth: `src/aibroker/routing/chains.py`.

The chain answers "for capability X, in what order do we try providers?"
Walk top-down, first one that returns a usable response wins.

Current chains (free-first is the soft rule; documented exceptions noted):

| Capability | Chain (left→right) | Notes |
|---|---|---|
| `chat:fast` | cerebras → groq → gemini → **deepseek** → openrouter → sambanova → nvidia → mistral → anthropic → openai | DeepSeek (paid, $0.3/1M) precedes slow openrouter:free for backfill throughput. Documented exception. |
| `chat:smart` | cerebras → groq → gemini → sambanova → anthropic → openrouter → nvidia → mistral → openai → deepseek | Strict free-first; expensive providers last |
| `chat:code` | cerebras → groq → openrouter → gemini → nvidia → sambanova → anthropic → deepseek → openai | DeepSeek-coder reserved for genuinely paid fallback |
| `prefilter` | cerebras → groq → gemini → sambanova → nvidia → openrouter → mistral | No paid; cheap pre-filter only |
| `structured` | cerebras → groq → gemini → openrouter → sambanova → nvidia → mistral → anthropic → openai | All support strict json_schema |
| `vision` | gemini → anthropic → openai | Image input support required |
| `embedding` | voyage | Single provider for now |

## Selector

`src/aibroker/routing/selector.py:pick_and_reserve(provider, scope, require_tier)`

Single Postgres statement:

```sql
UPDATE api_keys SET last_used_at = now()
WHERE id = (
    SELECT id FROM api_keys
    WHERE provider = :provider
      AND is_active = TRUE
      AND is_alive = TRUE
      AND scopes ? :scope
      AND (cooldown_until IS NULL OR cooldown_until < now())
      AND (daily_cost_cap_usd IS NULL
           OR daily_cost_used_usd < daily_cost_cap_usd)
      AND (daily_limit = 0 OR daily_used < daily_limit)
    ORDER BY last_used_at NULLS FIRST, id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING *
```

This is atomic, race-free across replicas, and advances LRU in one go.

## Cost guard

`src/aibroker/routing/cost_guard.py:check_caps(api_key, project, estimated_cost)`

Runs BEFORE every paid call. Three independent caps:

1. **Per-key**: `daily_cost_used + estimate > daily_cost_cap_usd` → block.
2. **Per-project**: live SUM from `usage_log` (today, this project) +
   estimate > `projects.daily_cost_cap_usd` → block.
3. **Global**: cached SUM from `usage_log` (today, all projects) +
   estimate > `GLOBAL_DAILY_CAP_USD` → block.

Free-tier keys with `cost == 0` skip the check entirely.

The global cache TTL is 30s — an upper bound of error per replica. Tighter
TTL means more `SUM(cost_usd)` queries; looser means more risk of overshoot
under a sudden burst. 30s was chosen empirically.

### Portability note

"Today" queries use `created_at >= :start AND created_at < :next_day` rather
than `created_at::date = :today`. Postgres-only `::date` casting was replaced
to make tests pass on SQLite in-memory.

## Failure → next provider

Selector returns a row → LiteLLM call → exception?

- `429` or rate-limit error string → `mark_cooldown(api_key_id, now+5min)`.
- `401/403` or auth error string → `mark_dead(api_key_id)`.
- Any other → `usage_log.status='error'`, `error_kind=type(e).__name__`.

In all failure cases, the route handler walks to the next provider in the
chain. If the chain is exhausted, returns `503 Service Unavailable`.
