"""Selector — atomic LRU + cap-aware key picker.

Skipped on SQLite: selector relies on JSONB `?` operator + FOR UPDATE SKIP LOCKED,
neither of which SQLite supports. These tests need a real Postgres to run.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import insert

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.routing.selector import (
    SelectionError,
    mark_cooldown,
    mark_dead,
    pick_and_reserve,
    record_usage,
)


pytestmark = pytest.mark.skipif(
    "sqlite" in os.environ.get("DATABASE_URL", ""),
    reason="Selector uses Postgres-specific JSONB ? operator + FOR UPDATE SKIP LOCKED",
)


async def _add_key(provider: str, label: str, **kw) -> int:
    """Insert one row, return its id."""
    defaults = dict(
        provider=provider, label=label, tier="free",
        scopes=["llm:chat"], token_encrypted="dummy",
        is_active=True, is_alive=True,
        daily_limit=999999, daily_used=0,
        daily_cost_used_usd=0.0, monthly_cost_used_usd=0.0, total_cost_usd=0.0,
        error_count=0, notes="",
    )
    defaults.update(kw)
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).returning(ApiKeyRow.id), defaults)
        return int(r.scalar_one())


async def test_pick_none_when_no_keys():
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_returns_lru_oldest_first():
    await _add_key("cerebras", "a", last_used_at=datetime.now() - timedelta(hours=2))
    await _add_key("cerebras", "b", last_used_at=datetime.now() - timedelta(hours=1))
    picked = await pick_and_reserve("cerebras", "llm:chat")
    assert picked is not None
    assert picked.label == "a"  # oldest used


async def test_pick_skips_inactive():
    await _add_key("cerebras", "x", is_active=False)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_dead():
    await _add_key("cerebras", "x", is_alive=False)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_in_cooldown():
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await _add_key("cerebras", "x", cooldown_until=future.replace(tzinfo=None))
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_capped():
    await _add_key("cerebras", "x", tier="paid",
                    daily_cost_cap_usd=1.0, daily_cost_used_usd=1.0)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_filters_by_scope():
    await _add_key("voyage", "x", scopes=["llm:embed"])
    result = await pick_and_reserve("voyage", "llm:chat")
    assert result is None   # wrong scope
    result = await pick_and_reserve("voyage", "llm:embed")
    assert result is not None


async def test_mark_cooldown_sets_future():
    kid = await _add_key("cerebras", "x")
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    await mark_cooldown(kid, future)
    # Subsequent pick should skip it
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_mark_dead_skips_subsequent_picks():
    kid = await _add_key("cerebras", "x")
    await mark_dead(kid)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_record_usage_increments_counters():
    kid = await _add_key("cerebras", "x", tier="free")
    await record_usage(
        api_key_id=kid, project_id=None, lease_id=None,
        provider="cerebras", model="gpt-oss-120b",
        capability="chat:fast", workflow="test",
        tokens_in=100, tokens_out=50, cost_usd=0.01,
        latency_ms=200, status="ok", error_kind=None, http_status=200,
    )
    async with get_session() as s:
        row = await s.get(ApiKeyRow, kid)
    assert row.daily_used == 1
    assert abs(row.daily_cost_used_usd - 0.01) < 1e-9
    assert abs(row.total_cost_usd - 0.01) < 1e-9
