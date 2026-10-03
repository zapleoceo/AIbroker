"""record_usage runs on BOTH dialects (2026-07-16): one data-modifying CTE on
Postgres, the two-statement fallback on SQLite (whose CTEs are SELECT-only).
Same assertions either way, so the SQLite quality gate covers the fallback and
the Postgres integration job covers the CTE."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, UsageLogRow
from aibroker.routing.selector import mark_cooldown, record_usage


async def _add_key(provider: str = "cerebras", label: str = "x") -> int:
    async with get_session() as s:
        key = ApiKeyRow(
            provider=provider, label=label, tier="free",
            scopes=["llm:chat"], token_encrypted=encrypt("dummy"),
            is_active=True, is_alive=True,
            daily_limit=999999, daily_used=0,
            daily_cost_used_usd=0.0, monthly_cost_used_usd=0.0,
            total_cost_usd=0.0, error_count=0, notes="",
        )
        s.add(key)
        await s.flush()
        return int(key.id)


async def _record(kid: int, **overrides) -> int:
    kwargs: dict = {
        "api_key_id": kid, "project_id": None, "lease_id": None,
        "provider": "cerebras", "model": "gpt-oss-120b",
        "capability": "chat:fast", "workflow": "test",
        "tokens_in": 100, "tokens_out": 50, "cost_usd": 0.01,
        "latency_ms": 200, "status": "ok", "error_kind": None, "http_status": 200,
    }
    kwargs.update(overrides)
    return await record_usage(**kwargs)


async def test_record_usage_returns_inserted_row_id():
    kid = await _add_key()
    usage_id = await _record(kid)
    assert isinstance(usage_id, int)
    async with get_session() as s:
        row = await s.get(UsageLogRow, usage_id)
    assert row is not None
    assert row.api_key_id == kid
    assert row.tokens_in == 100


async def test_record_usage_increments_counters():
    kid = await _add_key()
    await _record(kid)
    await _record(kid)
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.daily_used == 2
    assert abs(row.daily_cost_used_usd - 0.02) < 1e-9
    assert abs(row.total_cost_usd - 0.02) < 1e-9
    assert row.daily_reset_at is not None


async def test_record_usage_ok_clears_stale_error_state():
    kid = await _add_key(label="stale")
    await mark_cooldown(kid, datetime.now(UTC) + timedelta(minutes=10),
                        reason="rate limit")
    await _record(kid)
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.last_error is None
    assert row.error_count == 0
    assert row.cooldown_until is None


async def test_record_usage_error_keeps_failure_state():
    kid = await _add_key(label="failing")
    await mark_cooldown(kid, datetime.now(UTC) + timedelta(minutes=10),
                        reason="rate limit")
    await _record(kid, status="error", error_kind="RateLimit",
                  http_status=429, cost_usd=0.0)
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.last_error == "rate limit"
    assert row.cooldown_until is not None


# ─── model_served degradation flag (2026-10-03 review) ───────────────────────


def test_is_missing_model_served_only_for_undefined_column():
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from aibroker.routing.selector import _is_missing_model_served

    class _Orig(Exception):
        def __init__(self, msg, sqlstate=None):
            super().__init__(msg)
            self.sqlstate = sqlstate

    undefined = ProgrammingError("stmt", {}, _Orig('column "model_served" does not exist', "42703"))
    blip = OperationalError("stmt", {}, _Orig("connection was closed", "08006"))
    deadlock = OperationalError("stmt", {}, _Orig("deadlock detected", "40P01"))
    sqlite_missing = OperationalError("stmt", {}, Exception("table usage_log has no column named model_served"))
    sqlite_other = OperationalError("stmt", {}, Exception("database is locked"))
    assert _is_missing_model_served(undefined)
    assert not _is_missing_model_served(blip)
    assert not _is_missing_model_served(deadlock)
    assert not _is_missing_model_served(sqlite_other)
    # SQLite has no SQLSTATE: its INSERT error text names the column.
    assert _is_missing_model_served(OperationalError(
        "stmt", {}, Exception("no such column: model_served")))
    assert _is_missing_model_served(sqlite_missing)


async def test_transient_error_does_not_disable_model_served_for_the_process(monkeypatch):
    """REGRESSION (2026-10-03): ANY ProgrammingError/OperationalError flipped
    _model_served_available=False until restart, silently dropping the exact
    served model from every later row after one network blip."""
    from sqlalchemy.exc import OperationalError

    from aibroker.routing import selector

    monkeypatch.setattr(selector, "_missing_usage_cols", set())
    calls = {"n": 0}
    real_get_session = selector.get_session

    def flaky_get_session():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("stmt", {}, Exception("server closed the connection unexpectedly"))
        return real_get_session()

    monkeypatch.setattr(selector, "get_session", flaky_get_session)
    monkeypatch.setattr("aibroker.db.resilience._BASE_DELAY_S", 0.0)
    kid = await _add_key()
    usage_id = await _record(kid, model_served="exact-model")
    assert "model_served" not in selector._missing_usage_cols   # blip did not flip it
    async with get_session() as s:
        row = await s.get(UsageLogRow, usage_id)
    assert row.model_served == "exact-model"


async def test_missing_column_still_degrades_and_writes_the_row(monkeypatch):
    from sqlalchemy import text

    from aibroker.routing import selector

    monkeypatch.setattr(selector, "_missing_usage_cols", set())
    async with get_session() as s:
        await s.execute(text("ALTER TABLE usage_log DROP COLUMN model_served"))
    kid = await _add_key()
    usage_id = await _record(kid, model_served="x")
    assert isinstance(usage_id, int)
    assert "model_served" in selector._missing_usage_cols

async def test_record_usage_stores_the_request_id():
    """Every attempt row of one client request shares request_id (the fallback
    trail), so the rows of a request can be grouped."""
    kid = await _add_key()
    a = await _record(kid, request_id="req-trace-0001", status="error", error_kind="RateLimitError")
    b = await _record(kid, request_id="req-trace-0001")
    c = await _record(kid)                                  # no request scope: NULL
    async with get_session() as s:
        rows = {r: (await s.get(UsageLogRow, r)).request_id for r in (a, b, c)}
    assert rows == {a: "req-trace-0001", b: "req-trace-0001", c: None}


def test_insert_sql_drops_only_the_columns_the_schema_lacks():
    from aibroker.routing.selector import _insert_sql

    full = _insert_sql()
    assert "model_served" in full and "request_id" in full and ":rq" in full
    no_trace = _insert_sql(missing=frozenset({"request_id"}))
    assert "request_id" not in no_trace and "model_served" in no_trace
    neither = _insert_sql(missing=frozenset({"request_id", "model_served"}))
    assert "model_served" not in neither and "request_id" not in neither
