# Architecture

## Big picture

```
┌─── client projects ────┐
│ Vera, Stepan, …        │  HTTPS, X-Project-Key
└──────────┬─────────────┘
           ▼
┌──────────────────────────────────────────────────────┐
│ aibroker-api (FastAPI, async)                        │
│   POST /v1/chat?capability=...   → LiteLLM SDK       │
│   POST /v1/embed?provider=...    → LiteLLM SDK       │
│   POST /v1/key, /v1/usage, /v1/release  (vending)    │
│   GET  /v1/health, /admin/*, /dashboard, /login      │
└──────────┬───────────────────────────────────────────┘
           │ async pool
           ▼
┌─── aibroker-postgres ─────────────────────┐
│ projects, api_keys, leases, usage_log,    │
│ audit_log                                 │
└───────────────────────────────────────────┘
           ▲
           │
┌──── aibroker-monitor (loop) ─────────────┐
│ every 600s pings each key with the       │
│ cheapest provider call. Marks dead /     │
│ sets cooldown. Telegram alerts on state  │
│ flip via @aibzapleo_bot.                 │
└───────────────────────────────────────────┘
```

## Operating modes

### Proxy mode (default for LLM)
`POST /v1/chat`, `POST /v1/embed` — broker holds the keys, calls the provider
through LiteLLM, returns text + cost meta. Client never sees the API key.

### Vending mode (for non-LLM HTTP APIs)
`POST /v1/key` returns the plain key with a short TTL lease. Client calls the
provider directly, reports usage back via `POST /v1/usage`. Used when the
broker doesn't know the wire format (e.g. weird custom auth flows).

## Request flow (proxy mode)

1. Client sends `POST /v1/chat?capability=chat:fast` with `X-Project-Key`.
2. `auth.require_project` looks up project by hashed key, attaches scopes.
3. `routing.chains.chain_for("chat:fast")` returns ordered provider list.
4. For each provider:
   - `routing.selector.pick_and_reserve` does atomic `SELECT FOR UPDATE
     SKIP LOCKED` over alive/in-cap keys, picks the LRU oldest, touches
     `last_used_at` in the same TX.
   - `routing.cost_guard.check_caps` validates per-key + per-project +
     global daily caps against the estimated cost (0 for free tier).
   - `providers.litellm_adapter.call_llm` invokes LiteLLM.
   - On `429` → set cooldown 5 min; on `401/403` → mark dead.
   - On success → `selector.record_usage` writes usage_log + bumps
     daily_used / cost counters in the same TX.
5. Walks to next provider in chain on failure. Returns 503 if all exhausted.

## Scaling story

- API is stateless — all state in Postgres. Add replicas behind a load
  balancer; concurrent picks are safe because of `SKIP LOCKED`.
- Monitor is a single instance loop. If we need multiple, gate ticks with
  Postgres advisory locks.
- Postgres is the bottleneck. Vertical scaling fine until >1k qps; then
  read-replica for `usage_log` aggregation queries.
