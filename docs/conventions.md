# Conventions

## File layout

```
src/aibroker/
  main.py              FastAPI app + lifespan
  config.py            Pydantic settings (env-driven, lru_cached)
  auth.py              X-Project-Key + X-Admin-Key + scope guards
  auth_session.py      Telegram login + HMAC cookie
  crypto.py            Fernet at-rest
  db/
    engine.py          Async engine + sessionmaker + Base
    models.py          ORM models
    resilience.py      retry_terminal_write — retry transient failures on money-already-spent writes
  routing/
    chains.py          Capability → provider order
    selector.py        LRU + atomic pick_and_reserve
    cost_guard.py      Three-tier cap check
    cooldown.py        Adaptive backoff from the provider's own signal
    shared_state.py    Redis-shared affinity + saturation (fail-open)
  services/
    llm_service.py     run_chat/run_embed/run_transcribe orchestration
    job_queue.py       dispatcher_loop — claims + drains pending jobs
    deep_jobs.py       submit/poll + payload-hash dedup
    response_cache.py  exact-match LRU+TTL for translate/prefilter
  providers/
    litellm_adapter.py LLM SDK wrapper
    provider_errors.py Error classification (sign tables + verdicts)
    health_probes.py   Cheapest call per provider
  routes/
    health.py          / + /healthz + /v1/health
    proxy.py           /v1/jobs + /v1/embed
    admin.py           /admin/* (X-Admin-Key)
    dashboard*.py      /login + /dashboard + /dashboard/* (split: dashboard.py + dashboard_assets/data/render/scopes.py)
  telemetry/
    audit.py           append-only audit_log writer
    notifier.py        Telegram alert + throttle
  scripts/
    bootstrap.py       create admin-ops project
  monitor.py           background health-probe loop (separate container)
```

One responsibility per file, ~200 line ceiling. If a file grows past 300,
split.

## Async everywhere

- All DB and HTTP I/O is async.
- Never `asyncio.sleep` in a polling loop — diagnose the root cause.
- Use `async with get_session() as s` — never reuse sessions across calls.

## Types

- Type hints on every function signature.
- `X | None`, not `Optional[X]`.
- `list[X]`, `dict[K, V]`, not `List` / `Dict`.

## Comments

- No comments that explain *what* — names do that.
- Comments only for *why* (workaround, constraint, invariant).
- One-line docstrings or none. Never multi-paragraph.

## Errors

- Raise specific exceptions with clear messages.
- `WARNING` for expected transient failures (rate limits, timeouts).
- `ERROR/EXCEPTION` only for unexpected.
- Never swallow exceptions silently.

## SQLAlchemy

- `async with get_session() as s` — never reuse sessions.
- `session.execute(select(...))` not legacy `session.query(...)`.
- Commit explicitly (or rely on `get_session()`'s context manager).

## FastAPI routes

- Validate input via Pydantic.
- Thin handler: validate → call service → return dict or model.
- `Depends(require_admin)` or `Depends(require_project)` on every guarded route.
- Audit-log significant ops.

## Dashboard timestamps

- Every timestamp shown in the dashboard is rendered server-side as **UTC** inside
  `_ts_span(dt, tf)` → `<span class="ts" data-utc="…Z" data-tf="…">UTC fallback</span>`.
- The dashboard JS (`_DASHBOARD_JS`) rewrites each `span.ts[data-utc]` into the
  **viewer's** local timezone (`toLocaleString`) on load, so the operator reads
  times in their own zone, not the server's. No-JS falls back to the UTC text;
  the raw UTC stays in the span's `title`.
- Naive-UTC datetimes (as stored) get a trailing `Z` so `new Date` parses them as
  UTC, not local. `data-tf` (`hm`/`mdhm`/`mdhms`) mirrors the JS `F` format map.
- Day-bucketed figures ("today", the selected range, per-key quota bars) also
  align to the **viewer's** calendar day: the JS sets an `aib_tz` cookie
  (`Intl…timeZone`) and reloads once on first visit; the server resolves it via
  `dashboard_time.client_tz` (falls back to the `UTC_TZ` constant for a
  missing/invalid value); `today_in` + `day_bounds_utc` then shift every day
  boundary to that zone. This stays on the naive-UTC
  `created_at` column (Python-side bounds, no `AT TIME ZONE`), so it's
  DB-portable and unit-tested on SQLite. `dashboard_time.py` is the single source
  of truth for every dashboard day boundary. Needs `tzdata` (a dependency) so
  `ZoneInfo` resolves inside the slim container.

## Dashboard assets (cache-busting)

- The dashboard CSS/JS are served long-cached (`immutable`, 1y). Their `?v=`
  query is `dashboard_assets.ASSETS_VERSION` — a **hash of the CSS+JS content**,
  NOT the package `__version__`. So any asset edit auto-invalidates the browser
  cache; editing CSS/JS needs no manual version bump (relying on `__version__`
  once shipped JS to the server that never reached browsers).

## Tests

- One test file per source module: `test_<module>.py`.
- Fixtures in `tests/conftest.py`.
- In-memory SQLite for unit tests; mark DB-dependent tests with `@pytest.mark.asyncio`.
- Aim for >70% line coverage on `src/aibroker/` (current gate 70%, stair-step — never drops).
- Integration tests for routes use FastAPI `TestClient`.

## Pre-commit hooks

Optional but recommended — same checks CI runs:

```bash
pip install pre-commit
pre-commit install            # ruff + format on every commit
pre-commit install -t pre-push  # plus pytest on push
```

`.pre-commit-config.yaml` is in the repo root. Hooks: trailing whitespace,
EOL fixer, YAML syntax, large-file guard, merge-conflict markers,
`detect-private-key` (blocks accidental .pem / id_rsa commits), `ruff
--fix`, `ruff format`, `pytest -x` on push.
