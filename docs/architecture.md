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

**Cooldown revives a dead key too (2026-07-03).** `monitor.tick()`'s three
verdicts (`alive`/`cooldown`/`dead`) used to treat `cooldown` (429) as
orthogonal to `is_alive` — only a clean `alive` verdict reset `is_alive=True`.
A key marked dead once could get stuck there forever: `pick_and_reserve`
excludes `is_alive=False` from real traffic, so only the monitor's own tiny,
infrequent probe could prove it alive again — and if *that* kept landing on a
429 window (a real risk for tight-quota trial keys, e.g. cohere), the key
never got the clean `alive` reading it needed. A 429 already proves the
credential is valid (auth passed, just rate-limited) — `cooldown` now also
sets `is_alive=True` and fires the same `recover()` alert as `alive` does.

## Operating modes

### Proxy mode (default for LLM)
`POST /v1/chat`, `POST /v1/embed` — broker holds the keys, calls the provider
through LiteLLM, returns text + cost meta. Client never sees the API key.

### Vending mode (for non-LLM HTTP APIs)
`POST /v1/key` returns the plain key with a short TTL lease. Client calls the
provider directly, reports usage back via `POST /v1/usage`. Used when the
broker doesn't know the wire format (e.g. weird custom auth flows).

## Request flow (proxy mode)

Routes are thin (`routes/proxy.py`): authenticate, gate scope, delegate to
`services/llm_service`, shape the response. All orchestration lives in the
service (SRP — no business logic in the route layer).

1. Client sends `POST /v1/chat?capability=chat:fast` with `X-Project-Key`.
2. `auth.require_project` looks up project by hashed key, attaches scopes.
3. The route checks `is_known_capability` (else 400) and that the project holds
   `scope_for(capability)` (else 403) — so `vision` needs `llm:vision`,
   `chat:edit` needs `llm:edit`, not a blanket `llm:chat`.
4. `services.llm_service.run_chat` walks `chain_for(capability)`. For each
   provider:
   - `selector.pick_and_reserve(provider, scope_for(capability))` does atomic
     `SELECT … FOR UPDATE SKIP LOCKED`, ordering `is_reserve, last_used_at` so
     reserved keys are picked last (the reserved-lane mechanism). Touches
     `last_used_at` in the same TX.
   - `cost_guard.check_caps` validates per-key + per-project + global daily caps.
   - `litellm_adapter.call_llm` invokes LiteLLM (gemini+JSON also gets
     `reasoning_effort=disable` so thinking doesn't truncate the object).
   - `classify_provider_error`: 429 → cooldown 5 min; 401/403 → mark dead.
   - JSON quality gate: a JSON request whose body doesn't parse is billed but
     treated as a failure → next provider.
   - On success → `selector.record_usage` writes usage_log + bumps counters.
5. Walks to the next provider on failure. Returns 503 if all exhausted. The
   success response includes the chosen key's `key_label` for client-side
   cost/usage display.

## Tests & CI

`.github/workflows/ci.yml` runs on every push/PR: a **unit** job on in-memory
SQLite (the bulk), and an **integration** job against a real `postgres:16`
service so the Postgres-only paths — selector, reserved-lane, vending, monitor,
bootstrap — actually execute. `conftest` binds the engine to `DATABASE_URL`
(NullPool on Postgres so the sync TestClient's event-loop portal doesn't collide
with pooled asyncpg connections). The `deploy.yml` test gate keeps the coverage
floor on master.

## Dashboard

`/dashboard` (Telegram-login or `X-Admin-Key`) renders single-page HTML:

- KPI cards: spend today vs global cap, calls 1h, project count, key count.
- Provider summary line (alive / dead / total per provider) + a red
  **⚠N/1h** badge showing last-hour error count per provider, so a 429-storm
  is visible at a glance instead of only in the logs.
- Projects table — `id, name, scopes, active, daily cap, key prefix,
  actions`. Each row has inline **edit** that swaps the row for a form
  with `name`, `allowed_scopes` (checkbox group — same `_scope_checkboxes`
  widget the key forms use, validated against `_KNOWN_SCOPES`; was a raw
  comma-separated text input with no validation until 2026-07-05),
  `daily_cost_cap_usd`, `owner_email`.
- API keys table — `id, provider, label, tier, status, used, $/cap, errs,
  actions`. The `$/cap` cell shows `used / cap` with a coloured progress
  bar (blue < 70 % → yellow < 90 % → red). Inline **edit** form lets
  the operator rename the key, change tier/scope/cap, and optionally
  rotate the token in one shot. Old buttons (enable/disable/delete)
  stay.
  **Status detail (2026-07-05).** `status` used to be just "жив"/"пауза"/
  "мёртв" with no way to tell *why* — "no money" and "rate limited" both
  showed as a generic red/yellow pill, and a paused key gave no hint of
  when it'd recover. `api_keys.last_error` (short human reason: a probe
  hint like `no funds`/`rate limit`, or the real exception text truncated
  to 200 chars) is now set by both real-traffic failures
  (`services/llm_service.py:_penalize`) and the background monitor
  (`monitor.py:tick`), and cleared back to `NULL` the moment a key is
  confirmed alive again. Dead keys show the reason; cooldown keys show the
  reason **and** the actual `cooldown_until` time (same day → `HH:MM UTC`,
  otherwise `MM-DD HH:MM UTC`) — both as a small line under the pill and
  as a hover tooltip. **Friendly labels (2026-07-05):** `_friendly_reason`
  maps known raw signatures to a short actionable EN/RU label instead of a
  raw litellm dump — e.g. Anthropic's "credit balance is too low" (and
  other billing-exhaustion phrasings) renders as "top up balance"/
  "пополнить баланс", DeepSeek's "response_format type is unavailable" as
  "provider feature outage"/"сбой фичи у провайдера". Display-layer only
  (doesn't affect `classify_provider_error`'s routing decisions) — an
  unrecognized error still falls back to a truncated raw-text slice, and
  the full raw text is always in the tooltip regardless.
- All table headers are clickable for client-side sort (asc → desc → asc).
  Each cell uses `data-sort` for the canonical comparable value, so
  monetary or status text doesn't break ordering.

**`_gather_data` performance (2026-07-01).** The all-time default load (no
date filter) was taking up to ~30s once `usage_log` passed ~450k rows:
`range_stats` and `proj_spend` each did a separate full-table `SUM`, and
`created_at::date = ...` casts made the "calls last 1h" / "tokens today"
queries non-sargable even with an index. Fixed without changing the response
shape:

- `range_stats` + `proj_spend` merged into **one** `GROUP BY project_id` scan;
  the range-wide grand total is a cheap in-Python sum over the handful of
  per-project rows, not a second table scan.
- Date-range and "today" bounds are computed in Python as half-open
  `created_at >= start AND created_at < end`, never `::date`-cast — sargable
  against a plain `(created_at)` index (migration 005, `CONCURRENTLY`).
  **Gap found and fixed 2026-07-05:** migration 005 was written and
  documented here but had never actually been run against prod — only the
  three composite indexes existed, none of which lead with `created_at`
  alone, so the "calls last 1h" / "tokens today" queries were still doing
  a near-full-table index scan. Applied live: one such query went from
  264ms to 0.88ms on 850k+ rows.
- The 6 independent queries (projects, keys, range+proj totals, calls/1h,
  tokens/today, provider summary) run concurrently via `asyncio.gather`, each
  on its own pooled connection (`get_session()` per fetch) — a single
  `AsyncSession` can't run overlapping statements. `pool_size=10 +
  max_overflow=20` comfortably covers 6 concurrent connections per load.

**Project drill-down** (`/dashboard/projects/{id}?range=24h|7d|30d`): KPI cards
(calls, spend, tokens, avg latency + success %, prompt-cache when active),
breakdown cards by provider / capability / **workflow** / model, and a
**latency-distribution histogram** (calls per latency bucket: `<250ms … >30s`,
bars scaled to the busiest bucket). The by-workflow card attributes cost/calls
to each caller task (`triage`, `rel_extract`, `coach_edit`, …) — the data was
always in `usage_log.workflow`, now surfaced so "where are we spending" isn't a
manual query.
**Every aggregate is scoped to the selected range** — only the "recent 50
calls" table ignores it. The histogram surfaces a slow tail that a single
average hides (e.g. an avg of 6 s that is really fast calls plus a fat `>30s`
timeout bucket).

There is no separate "status mix" breakdown: `usage_log.status` only ever
takes two values (`ok`/`error`), so a per-status GROUP BY would just duplicate
the ok/err split already on the Calls KPI card — via a second query, no less.
Removed 2026-07-01 rather than kept as a redundant tile.

All form posts go through `require_owner_session`; an unauth POST returns
401. Every mutation writes an `audit_log` row through
`telemetry.audit()`.

**Static shell vs data (2026-07-05).** The ~17KB of CSS + JS behind the
dashboard never changes per request, but used to be inlined into the same
HTML document as live key/project/usage data — and the whole document is
served `Cache-Control: no-store` (the data must never be Chrome-heuristic-
cached, see the login-page no-store note above). Every navigation was
re-downloading and re-parsing identical markup. Split into
`_DASHBOARD_CSS`/`_DASHBOARD_JS`, served from `GET /dashboard/assets.css`
and `GET /dashboard/assets.js` — both public (no user data in them) and
long-cached (`Cache-Control: public, max-age=31536000, immutable`),
versioned via `?v={__version__}` in `_dash_html` so a deploy naturally
busts the cache. The HTML document itself still `<link>`s/`<script src>`s
these and stays `no-store`.

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

### Per-key daily-quota progress bar

The keys table's `daily %` column shows where each key sits against its
provider's free-tier daily quota. Driven by `providers/quotas.py`:
each `Quota` carries `req_per_day`, `tok_per_day`, and a `doc` URL to the
provider's rate-limit page (for verification — these numbers drift).

Both axes apply at once for providers that meter both (e.g. Cerebras:
14,400 req/day AND 1M tokens/day). `percent_used(req, tok, provider)`
returns the **max** of the two — whichever you'll hit first wins.
Token usage is summed live from `usage_log` (today UTC) per `api_key_id`,
so the bar reflects real consumption, not stale counters.

**Per-key manual quota override** (2026-06-28, add-form 2026-06-29): both
the dashboard **add-key** and **edit-key** forms expose four number inputs —
`req/day`, `tok/day`, `in/day`, `out/day` — alongside the `$ cap`.
These set `api_keys.manual_*_limit` via one shared writer,
`_apply_manual_limits(key, req, tok, tok_in, tok_out)` (each axis parsed by
`_positive_int_or_none`: blank / 0 / negative → NULL = no cap), called from
add-create, upsert and edit so the four axes stay in lock-step everywhere.
Resolution per axis is
**manual > discovered > PROVIDER_QUOTAS default** (`quota_for_key`). The
in/out split exists specifically for asymmetric corporate keys — e.g. a
Gemini key capped at 3M input / 80k output tokens per day: its 80k output
axis saturates long before the 3M input axis, and the selector + bar both
track each axis independently. The selector's saturation `CASE` checks all
four axes; ≥95 % on **any** axis pushes the key to the back of its bucket.
The bar tooltip tags the source: `manual` / `discovered` / `default est.`.

**Per-key auto-discovery** (2026-06-28): when a key is created via
`POST /admin/keys` or the dashboard add form, the broker probes it once
and parses the response's rate-limit headers
(`x-ratelimit-limit-requests-day`, `anthropic-ratelimit-tokens-limit`,
etc. — provider-specific map in `providers/health_probes.extract_quota_headers`).
Real numbers are stored on `api_keys.discovered_req_limit /
discovered_tok_limit / limits_discovered_at`, and `quota_for_key()`
prefers them over the static `PROVIDER_QUOTAS` defaults. Bar tooltip
shows `' · discovered'` vs `' · default est.'` so the operator knows
which source drove the percentage.

Discovered by hard experience: my first version only counted requests,
and the dashboard happily showed Cerebras at 6 % while three keys were
already past 100 % of the token quota (Cerebras emailed user a
'90 % free tokens' alert before the dashboard caught up). Fixed
2026-06-28; regression test `test_main_render_keys_show_token_axis_when_dominant`
locks it in.

Render: `bar_label()` shows whichever axis dominates — `'525/14400'`
(requests) or `'1.4M/1M tok'` (tokens). Bar coloured by
`severity_class()` (blue <70 %, yellow 70-89 %, red ≥90 %). Tooltip
exposes both axes (`'97 % · 525 req · 1,356,576 tok'`) for debugging.

Sortable by combined percentage via `data-sort`; paid keys get the
sentinel `-1` so they cluster at one end.

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
