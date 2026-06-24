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
  routing/
    chains.py          Capability → provider order
    selector.py        LRU + atomic pick_and_reserve
    cost_guard.py      Three-tier cap check
  providers/
    litellm_adapter.py LLM SDK wrapper
    health_probes.py   Cheapest call per provider
  routes/
    health.py          / + /healthz + /v1/health
    proxy.py           /v1/chat + /v1/embed
    vending.py         /v1/key + /v1/usage + /v1/release
    admin.py           /admin/* (X-Admin-Key)
    dashboard.py       /login + /dashboard + /dashboard/* form handlers
  telemetry/
    audit.py           append-only audit_log writer
    notifier.py        Telegram alert + throttle
  scripts/
    bootstrap.py       create admin-ops project
    migrate_from_vera.py  one-shot key import
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

## Tests

- One test file per source module: `test_<module>.py`.
- Fixtures in `tests/conftest.py`.
- In-memory SQLite for unit tests; mark DB-dependent tests with `@pytest.mark.asyncio`.
- Aim for >70% line coverage on `src/aibroker/` (gate currently 40%).
- Integration tests for routes use FastAPI `TestClient`.
