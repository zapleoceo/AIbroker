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

## Dashboard

`/dashboard` (Telegram-login or `X-Admin-Key`) renders single-page HTML:

- KPI cards: spend today vs global cap, calls 1h, project count, key count.
- Provider summary line (alive / dead / total per provider).
- Projects table — `id, name, scopes, active, daily cap, key prefix,
  actions`. Each row has inline **edit** that swaps the row for a form
  with `name`, `allowed_scopes` (csv), `daily_cost_cap_usd`,
  `owner_email`.
- API keys table — `id, provider, label, tier, status, used, $/cap, errs,
  actions`. The `$/cap` cell shows `used / cap` with a coloured progress
  bar (blue < 70 % → yellow < 90 % → red). Inline **edit** form lets
  the operator rename the key, change tier/scope/cap, and optionally
  rotate the token in one shot. Old buttons (enable/disable/delete)
  stay.
- All table headers are clickable for client-side sort (asc → desc → asc).
  Each cell uses `data-sort` for the canonical comparable value, so
  monetary or status text doesn't break ordering.

All form posts go through `require_owner_session`; an unauth POST returns
401. Every mutation writes an `audit_log` row through
`telemetry.audit()`.

### Add-key form is provider-driven

The `<select>` for `provider` in the Add-key form is built from
`_provider_catalogue()`, which reads `providers.litellm_adapter.DEFAULT_MODEL`
— there is **one source of truth** for "what providers we support."
Adding a provider/model entry there immediately surfaces it in the
dashboard dropdown; no separate frontend list to keep in sync.

When the operator picks a provider:

- the `scope` select auto-flips to `llm:embed` for voyage and
  `llm:chat` for chat-style providers (`default_scope` field)
- a hint panel under the form lists every capability the broker will
  route to this provider (`chat:fast`, `chat:smart`, `vision`, …) and
  the exact model id used per capability
- on EN↔RU toggle the hint re-renders in the new language

JSON describing the catalogue is inlined into `/dashboard` as
`<script type="application/json" id="provider-meta">`; the form JS reads
it and drives all the linked behaviour client-side. No round-trip per
keystroke.

### Bilingual UI (EN/RU)

The login page and the dashboard both ship every visible label in both
languages. EN is the default on first paint; an `EN/RU` toggle in the
top-right swaps `textContent` from `data-en` / `data-ru` attributes on
elements marked with `data-i18n`. Input placeholders use a parallel
`data-en-placeholder` / `data-ru-placeholder` pair. The choice is
persisted in `localStorage` under `aib_lang`; `?lang=ru` or `?lang=en`
in the URL takes precedence on the next paint (handy for sharing).

The toggle is pure client-side — no server round-trip. Adding a new
label means writing `data-i18n data-en="..." data-ru="..."` next to
the source string; no template engine, no .po files.

## Scaling story

- API is stateless — all state in Postgres. Add replicas behind a load
  balancer; concurrent picks are safe because of `SKIP LOCKED`.
- Monitor is a single instance loop. If we need multiple, gate ticks with
  Postgres advisory locks.
- Postgres is the bottleneck. Vertical scaling fine until >1k qps; then
  read-replica for `usage_log` aggregation queries.
