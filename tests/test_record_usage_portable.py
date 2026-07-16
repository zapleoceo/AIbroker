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
