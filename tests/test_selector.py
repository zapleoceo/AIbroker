"""Selector — atomic LRU + cap-aware key picker.

Skipped on SQLite: selector relies on JSONB `?` operator + FOR UPDATE SKIP LOCKED,
neither of which SQLite supports. These tests need a real Postgres to run.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow
from aibroker.routing.selector import (
    mark_cooldown,
    mark_dead,
    pick_and_reserve,
    record_usage,
)

_DB = os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    "postgres" not in _DB and "asyncpg" not in _DB,
    reason="Selector uses Postgres-specific JSONB ? operator + FOR UPDATE SKIP LOCKED",
)


async def _add_key(provider: str, label: str, **kw) -> int:
    """Insert one row, return its id."""
    defaults = {
        "provider": provider, "label": label, "tier": "free",
        "scopes": ["llm:chat"], "token_encrypted": "dummy",
        "is_active": True, "is_alive": True,
        "daily_limit": 999999, "daily_used": 0,
        "daily_cost_used_usd": 0.0, "monthly_cost_used_usd": 0.0,
        "total_cost_usd": 0.0, "error_count": 0, "notes": "",
    }
    defaults.update(kw)
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).returning(ApiKeyRow.id), defaults)
        return int(r.scalar_one())


async def test_pick_none_when_no_keys():
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_distributes_randomly_across_eligible_keys():
    """2026-06-28: LRU replaced by random() — over 100 picks both keys get
    real share of traffic instead of one monopolising. Reset last_used_at
    each iteration so neither becomes 'oldest'."""
    from sqlalchemy import update
    await _add_key("cerebras", "a")
    await _add_key("cerebras", "b")
    counts = {"a": 0, "b": 0}
    for _ in range(100):
        picked = await pick_and_reserve("cerebras", "llm:chat")
        assert picked is not None
        counts[picked.label] += 1
        # reset both back so neither dominates by LRU; only random() decides
        async with get_session() as s:
            await s.execute(update(ApiKeyRow).values(last_used_at=None))
    # With random rotation, expect roughly 50/50 ± 30 over 100 picks
    assert 20 <= counts["a"] <= 80, f"distribution skewed: {counts}"
    assert 20 <= counts["b"] <= 80, f"distribution skewed: {counts}"


async def test_pick_pushes_over_quota_key_to_back():
    """A key already burned >=95% of today's token quota should not be picked
    while a clean peer is eligible. Cerebras default tok_per_day=1_000_000;
    seed today's usage_log to push key 'hot' over the threshold."""
    from sqlalchemy import insert as sql_insert
    await _add_key("cerebras", "cold")
    hot = await _add_key("cerebras", "hot")
    # 1.1M tokens today on 'hot' → > 95% of 1M default cap
    async with get_session() as s:
        from aibroker.db.models import UsageLogRow
        await s.execute(sql_insert(UsageLogRow), [{
            "api_key_id": hot, "provider": "cerebras", "tokens_in": 1_100_000,
            "tokens_out": 0, "cost_usd": 0.0, "status": "ok",
        }])
    # 20 picks — none should land on 'hot' while 'cold' is alive
    cold_count = hot_count = 0
    from sqlalchemy import update
    for _ in range(20):
        picked = await pick_and_reserve("cerebras", "llm:chat")
        assert picked is not None
        if picked.label == "cold":
            cold_count += 1
        else:
            hot_count += 1
        async with get_session() as s:
            await s.execute(update(ApiKeyRow).values(last_used_at=None))
    assert hot_count == 0, f"saturated key still picked {hot_count} times"
    assert cold_count == 20


async def test_pick_respects_manual_tok_out_limit():
    """A key with a manual OUTPUT-token cap (e.g. corp Gemini 80k out) is
    skipped once today's tokens_out ≥95% of it — even though its total/in
    usage and provider defaults are nowhere near saturated. Proves the
    manual in/out axis is honoured in rotation."""
    from sqlalchemy import insert as sql_insert
    from sqlalchemy import update

    from aibroker.db.models import UsageLogRow
    await _add_key("gemini", "cold", scopes=["llm:chat"])
    hot = await _add_key("gemini", "hot", scopes=["llm:chat"],
                          manual_tok_out_limit=80_000)
    # 'hot' used 76k output today = 95% of its 80k manual out-cap.
    # tokens_in tiny, total tiny — only the OUT axis trips.
    async with get_session() as s:
        await s.execute(sql_insert(UsageLogRow), [{
            "api_key_id": hot, "provider": "gemini", "tokens_in": 100,
            "tokens_out": 76_000, "cost_usd": 0.0, "status": "ok",
        }])
    cold_count = hot_count = 0
    for _ in range(20):
        picked = await pick_and_reserve("gemini", "llm:chat")
        assert picked is not None
        if picked.label == "cold":
            cold_count += 1
        else:
            hot_count += 1
        async with get_session() as s:
            await s.execute(update(ApiKeyRow).values(last_used_at=None))
    assert hot_count == 0, f"out-saturated key still picked {hot_count} times"
    assert cold_count == 20


async def test_pick_falls_back_to_saturated_when_all_saturated():
    """When every alive peer is over-quota, picker still returns one
    (it's a soft-sort, not a hard exclude — better a maybe-throttled call
    than no call)."""
    from sqlalchemy import insert as sql_insert

    from aibroker.db.models import UsageLogRow
    a = await _add_key("cerebras", "a")
    b = await _add_key("cerebras", "b")
    async with get_session() as s:
        await s.execute(sql_insert(UsageLogRow), [
            {"api_key_id": a, "provider": "cerebras", "tokens_in": 2_000_000,
             "tokens_out": 0, "cost_usd": 0.0, "status": "ok"},
            {"api_key_id": b, "provider": "cerebras", "tokens_in": 2_000_000,
             "tokens_out": 0, "cost_usd": 0.0, "status": "ok"},
        ])
    picked = await pick_and_reserve("cerebras", "llm:chat")
    assert picked is not None
    assert picked.label in ("a", "b")


async def test_pick_skips_inactive():
    await _add_key("cerebras", "x", is_active=False)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_dead():
    await _add_key("cerebras", "x", is_alive=False)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_pick_skips_in_cooldown():
    future = datetime.now(UTC) + timedelta(minutes=10)
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
    future = datetime.now(UTC) + timedelta(minutes=5)
    await mark_cooldown(kid, future)
    # Subsequent pick should skip it
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_mark_dead_skips_subsequent_picks():
    kid = await _add_key("cerebras", "x")
    await mark_dead(kid)
    result = await pick_and_reserve("cerebras", "llm:chat")
    assert result is None


async def test_reserve_key_picked_only_when_shared_exhausted():
    """Reserve key is the safety net: shared edit keys go first; the reserve
    is picked only once every shared key in the group is unavailable."""
    shared = await _add_key("gemini", "shared", scopes=["llm:chat", "llm:edit"],
                            last_used_at=datetime.now() - timedelta(hours=1))
    await _add_key("gemini", "reserve", scopes=["llm:edit"], is_reserve=True,
                   last_used_at=datetime.now() - timedelta(hours=5))  # older, but reserve

    # Even though the reserve key is older (LRU would prefer it), the shared key wins.
    picked = await pick_and_reserve("gemini", "llm:edit")
    assert picked is not None
    assert picked.label == "shared"

    # Knock the shared key into cooldown → now the reserve is used.
    await mark_cooldown(shared, datetime.now(UTC) + timedelta(minutes=10))
    picked = await pick_and_reserve("gemini", "llm:edit")
    assert picked is not None
    assert picked.label == "reserve"


async def test_reserve_edit_key_invisible_to_chat_scope():
    """A key scoped only to llm:edit must never serve bot llm:chat traffic."""
    await _add_key("gemini", "reserve", scopes=["llm:edit"], is_reserve=True)
    assert await pick_and_reserve("gemini", "llm:chat") is None
    assert await pick_and_reserve("gemini", "llm:edit") is not None


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
