# Architecture

## Big picture

```
┌─── client projects ────┐
│ Vera, Stepan, …        │  HTTPS, X-Project-Key
└──────────┬─────────────┘
           ▼
┌──────────────────────────────────────────────────────┐
│ aibroker-api (FastAPI, async)                        │
│   POST /v1/jobs?capability=...   → queue → LiteLLM   │
│   POST /v1/embed?provider=...    → LiteLLM SDK       │
│                                                      │
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
│ every 600s pings keys due this sweep     │
│ with the cheapest provider call. Marks   │
│ dead / sets cooldown. Telegram alerts on │
│ state flip via @aibzapleo_bot.           │
└───────────────────────────────────────────┘
```

**Adaptive probe cadence (2026-07-12).** Probing EVERY key every sweep was
self-harm: 144 sweeps/day × ~75 keys ≈ 10.8k real `max_tokens=1` completions
a day spent on liveness alone. `_should_probe` now probes ALIVE keys only
every `_ALIVE_PROBE_EVERY_N=6` sweeps (once/hour); DEAD or in-cooldown keys
every sweep — they're the ones whose state needs re-confirmation, and
auto-revive depends on it. Micro-RPD skip: a provider whose effective
req/day quota (manual > discovered > `PROVIDER_QUOTAS` seed) is under
`_MIN_RPD_FOR_LIVE_PROBE=200` never gets an ALIVE key live-probed at all —
sambanova's `req_per_day=20` meant probes alone exceeded a key's entire
daily quota, and gemini free (~1500/day) lost ~10% of budget to probing;
dead/cooldown keys of those providers are still probed (reviving is worth
one call).

**Paid-tail alert (2026-07-12).** After the probe pass, `tick()` runs
`_check_paid_tail`: for each capability in `_PAID_TAIL_CAPS` (`chat:fast`,
`chat:smart` — the chains whose paid tail is the guaranteed-answer anchor,
see `test_chains.py`'s paid-tail invariant) it checks whether ANY provider in
`chain_for(cap)` still has at least one usable paid key — `is_active`,
`is_alive`, not in cooldown, correctly scoped, and not over its daily cost
cap (same freshness rule as `FRESH_DAILY_COST_SQL`). If none is left, the
capability is silently free-only — a throttled Telegram alert fires, keyed
`paid_tail:<capability>` so the notifier's `recover()` auto-clears it the
moment a paid key comes back.

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
`POST /v1/jobs` (chat, async submit+poll) and `POST /v1/embed` (sync) — broker
holds the keys, calls the provider through LiteLLM, returns text + cost meta.
Client never sees the API key. (Sync `POST /v1/chat` was removed 2026-07-10 —
`410 Gone` → use `/v1/jobs`.)

### Vending mode — REMOVED (2026-07-12)
`POST /v1/key` used to hand out plaintext provider tokens under a short lease
for providers the broker "didn't know the wire format" of. LiteLLM covers every
provider we run, the endpoint had zero production callers, and its body was an
untested plaintext-token-exfiltration surface — deleted (routes/vending.py,
its tests, `VENDING_RATE_LIMIT_PER_MINUTE`). The `leases` table and
`usage_log.lease_id` column stay in the DB as historical data — no destructive
migration.

## Terminal-write resilience (2026-07-12)

By the time the broker records a successful provider call, the money is already
spent — so the two terminal writes (`selector.record_usage`, deep-job
`_finish`) are wrapped in `db/resilience.retry_terminal_write`: up to 3
attempts with short backoff on TRANSIENT connection failures only
(OperationalError / InterfaceError / invalidated connections). Non-transient
errors (IntegrityError…) surface immediately — they're bugs, not blips. Before
this, a Postgres restart at the wrong instant produced a billed-but-unrecorded
call and a client 500 for a response the broker had already paid for.

`litellm` is PINNED (==1.92.0, pyproject + Dockerfile): the provider-error
classification (`classify_provider_error` + cooldown sign tables) is calibrated
against version-specific litellm behaviour — e.g. cohere quota-429 arriving as
`APIConnectionError`. A silent minor bump can reshuffle exception classes and
quietly break cooldown/failover; upgrades must re-run the integration suite
deliberately.

## Request flow (chat is async-only since 2026-07-10)

Routes are thin (`routes/proxy.py`): authenticate, gate scope, delegate to
`services/llm_service`, shape the response. All orchestration lives in the
service (SRP — no business logic in the route layer).

**Chat runs through the async job queue, not a sync call.** `POST /v1/chat`
was removed (returns `410 Gone`); clients submit `POST /v1/jobs?capability=X`
and poll `GET /v1/jobs/{id}`. The dispatcher (see "Async jobs" below) claims the
job and runs `run_chat` — the SAME orchestration described here. `embed` and
`transcribe` stay synchronous on `/v1/embed` / `/v1/transcribe` (fast, no proxy
read-timeout problem). So the steps below are what the DISPATCHER does per
claimed chat job:

1. Client submits `POST /v1/jobs?capability=chat:fast` with `X-Project-Key`;
   the dispatcher later claims the pending row.
2. `auth.require_project` (at submit) looks up project by hashed key, attaches scopes.
3. Submit checks the capability is a known async-job capability (else 400) and
   the project holds `scope_for(capability)` (else 403) — `vision` needs
   `llm:vision`, `chat:edit` needs `llm:edit`, not a blanket `llm:chat`.
4. `services.llm_service.run_chat` walks `chain_for(capability)`. For each
   provider:
   - `selector.pick_and_reserve(provider, scope_for(capability),
     project_id=…)` does atomic `SELECT … FOR UPDATE SKIP LOCKED`, ordering
     `is_reserve, saturated, affinity, random()` so reserved keys are picked
     last (the reserved-lane mechanism) and, among equally-eligible keys, the
     one that last served this (project, provider) wins — keeping the
     provider-side prompt cache warm (see **Selector** in
     [routing.md](routing.md), 2026-07-12). Touches `last_used_at` in the
     same TX.
   - `cost_guard.check_caps` validates per-key + per-project + global daily caps.
     The worst-case cost is RESERVED before the call and released after. A
     successful call books its real cost; a **timeout** books the reserved
     ESTIMATE (not $0) — the provider generated and billed a response we never
     received, so the daily cost cap must see that spend or it stays blind and
     never stops the key (fix 2026-07-12: Google billed $122 on the paid gemini
     key while the broker recorded $2; `is_timeout` gates this). Pre-processing
     rejects (429/auth/503) cost nothing and stay free.
   - `litellm_adapter.call_llm` invokes LiteLLM, applying the provider's
     **adapter** first (see below).
   - `classify_provider_error`: 429 → cooldown 5 min; 401/403 → mark dead.
   - JSON quality gate: a JSON request whose body doesn't parse is billed but
     treated as a failure → next provider.
   - On success → `selector.record_usage` writes usage_log + bumps counters.
5. Walks to the next provider on failure. Returns 503 if all exhausted. The
   success response includes the chosen key's `key_label` for client-side
   cost/usage display.

## Provider adapters (2026-07-10)

Providers differ in small, specific ways LiteLLM doesn't paper over: deepseek
rejects the strict `json_schema` sub-type (downgrade to `json_object`), gemini
2.5 needs `reasoning_effort=disable` on JSON so thinking doesn't truncate the
object, cloudflare needs an account-scoped `api_base`. Each quirk used to be
another `if provider == …` branch in `call_llm`. They now live one-per-provider
in `providers/adapters.py`: `adapter_for(provider)` returns a `ProviderAdapter`
with two no-op-by-default hooks — `prepare(model, kwargs)` (request-shape
quirks, applied inside `call_llm`) and `key_extra(account_id)` (per-key kwargs
like cloudflare's api_base, applied by `run_chat` via `extra_for_provider`).
Adding a provider's quirk is a new adapter class, not an edit to the shared
call path (open/closed). The default adapter is a no-op, so providers with no
quirks need no entry.

## Provider error classification (2026-07-12)

The error-verdict logic lives in `providers/provider_errors.py` (extracted from
`services/llm_service.py`): the incident-calibrated sign tables
(`_RATE_LIMIT_SIGNS`, `_AUTH_SIGNS`, `_BILLING_DEPLETED_SIGNS`, the
provider-scoped maps), `classify_provider_error(exc, provider)`,
`is_model_unavailable(exc)` (404/model-gone → skip provider, don't penalize the
key) and `is_timeout(exc)` (billable holds — see the cost-cap note above).
`llm_service` re-exports `classify_provider_error` for existing import sites;
the WHAT-to-do-about-it side (`_penalize`: cooldown vs mark_dead) stays in the
orchestrator.

## Async jobs — the drained queue (2026-07-10)

The chat path. Sync `/v1/chat` was removed (`410 Gone`); every chat now goes
through the drained queue, which guarantees an answer without holding a
connection (a slow/oversubscribed provider can 504 a sync call before the
broker finishes its fallback chain). See `docs/api.md`.

- **Submit = enqueue.** `POST /v1/jobs?capability=X` (`routes/proxy.py`) →
  `services/deep_jobs.submit_job` inserts one `pending` row in `deep_jobs` and
  returns a `job_id` immediately. That's it — a cheap durable INSERT that
  always succeeds, whatever the provider pool is doing. (`/v1/deep` is a
  backward-compatible alias for `capability=chat:deep`.)
- **Dispatcher = drain.** `services/job_queue.py`'s `dispatcher_loop` runs once
  per uvicorn worker (started from the app lifespan). Woken instantly by
  submit's `pg_notify('aib_jobs')` via a dedicated asyncpg LISTEN connection
  (2026-07-12 — kills the old up-to-1s claim-latency floor; a timed
  `_IDLE_POLL_INTERVAL_S`=5s poll stays as the fallback so a missed NOTIFY can
  never stall jobs; on SQLite no listener starts and it degrades to plain
  `_POLL_INTERVAL_S` polling), it claims up to `JOB_MAX_CONCURRENCY` eligible
  `pending` rows — an atomic
  `UPDATE … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING *`, so the
  workers never double-claim — flips them to `running`, and runs each through
  the SAME `run_chat` the sync path uses. On success → `done`; the client's
  next `GET /v1/jobs/{id}` poll reads the result row from Postgres (works on
  whichever worker answers).
- **Resilience (why a queue, not fire-and-forget).**
  - *Survives a deploy.* A `running` row whose worker died mid-call is detected
    (`started_at` past `_STALE_RUNNING_S`) and re-queued by the next tick —
    a few minutes of broker downtime delays answers, never drops them.
  - *Backpressure.* A flood of submits no longer means a flood of concurrent
    provider calls — at most `JOB_MAX_CONCURRENCY` per worker; the rest wait.
  - *Retries transient no-capacity.* If `run_chat` returns None (whole pool
    cooling), the job is re-queued with exponential backoff (`run_after`,
    `retry_count`) up to `JOB_MAX_RETRIES`, then errors. The queue drains as
    capacity frees up.
- `drain_once()` is one deterministic pass (claim + run to completion) — what
  the loop repeats, and what tests drive directly.
- Queue state lives on `deep_jobs` (migrations 008 `capability`, 009
  `started_at`/`retry_count`/`run_after` + claimable index). The DB-touching
  code is Postgres-only (SKIP LOCKED / `make_interval`), covered by the
  Postgres CI job (`tests/test_job_queue.py`) and `# pragma: no cover`'d for
  the SQLite diff-cover run.

## Tests & CI

`.github/workflows/ci.yml` runs on every push/PR: a **unit** job on in-memory
SQLite (the bulk), and an **integration** job against a real `postgres:16`
service so the Postgres-only paths — selector, reserved-lane, monitor,
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

**Project drill-down** (`/dashboard/projects/{id}?range=1h|4h|12h|24h|7d|30d`): KPI cards
(calls, spend, tokens, avg latency + success %, prompt-cache when active),
breakdown cards by provider / model, a combined **capability + workflow**
card (2026-07-10: merged into one `.brk-card` — capability on top, workflow
below, split by a horizontal rule — they're both small slices of the same
calls and don't need a full grid cell each), and a **latency-distribution
histogram** (calls per latency bucket: `<250ms … >30s`, bars scaled to the
busiest bucket). The workflow half attributes cost/calls to each caller task
(`triage`, `rel_extract`, `coach_edit`, …) — the data was always in
`usage_log.workflow`, now surfaced so "where are we spending" isn't a manual
query.

Each capability/workflow **row** also carries a mini ok/error histogram
(`_sparkline_svg`, 2026-07-10): `_SPARK_BUCKETS` (24) thin stacked bars —
blue=ok, red=error — spanning the selected range, so an error burst on one
specific capability (e.g. `chat:smart` timing out) is visible per-row without
switching to the latency histogram, which isn't filtered by capability/
workflow. `dashboard_data._fetch_type_sparklines(project_id, hours, column)`
buckets `usage_log` via `width_bucket(extract(epoch from created_at), …)`
into N equal-width time slices and counts ok/error per slice — one query for
capability, one for workflow (both small: buckets × distinct values, not raw
rows). Each row's bars scale to that row's OWN busiest bucket, not the
busiest across all rows, so a quiet workflow stays visible next to a loud one.
`_fetch_type_sparklines` is `# pragma: no cover` like the other Postgres-only
fetchers in `dashboard_data.py` (`_fetch_calls_1h`, `_fetch_provider_summary`)
— `now()`/`width_bucket`/`extract(epoch)` have no SQLite equivalent, so
diff-cover's SQLite run can't reach it; it's exercised by the Postgres-only
`test_fetch_type_sparklines_splits_ok_and_error_by_bucket`.
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
