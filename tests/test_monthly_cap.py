"""Monthly cost caps (project + key): enforced at reservation and in the key pick,
reset lazily on the UTC calendar month. Mirrors the daily-cap design.

Runs on SQLite (the CI gate) - the month start is a bound parameter, no
Postgres-only SQL. Only the refund path (GREATEST) needs Postgres.
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import insert, text

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.routing.cost_guard import CostGuardError, release_cost, reserve_cost
from aibroker.routing.selector import (
    MONTHLY_CAP_OK_SQL,
    month_params,
    month_start_utc,
    record_usage,
)

_DB = os.environ.get("DATABASE_URL", "")
ON_POSTGRES = "postgres" in _DB or "asyncpg" in _DB


def _project(*, monthly=None, pid=1) -> ProjectRow:
    return ProjectRow(id=pid, name="x", project_key_hash="x", project_key_prefix="x",
                      allowed_scopes=["llm:chat"], daily_cost_cap_usd=None,
                      monthly_cost_cap_usd=monthly, is_active=True, notes="")


_next_id = iter(range(100, 10_000))


async def _add_key(**kw) -> ApiKeyRow:
    kid = next(_next_id)
    d = {
        "id": kid, "provider": "x", "label": f"k{kid}", "tier": "paid",
        "scopes": ["llm:chat"], "token_encrypted": "x", "is_active": True,
        "is_alive": True, "daily_limit": 999999, "daily_used": 0,
        "daily_cost_used_usd": 0.0, "daily_cost_cap_usd": None,
        "daily_reset_at": date.today(), "monthly_cost_cap_usd": None,
        "monthly_cost_used_usd": 0.0, "monthly_reset_at": month_start_utc(),
        "total_cost_usd": 0.0, "error_count": 0, "notes": "",
    }
    d.update(kw)
    async with get_session() as s:
        await s.execute(insert(ApiKeyRow), d)
    return ApiKeyRow(**d)


async def _read(kid: int) -> ApiKeyRow:
    async with get_session() as s:
        return await s.get(ApiKeyRow, kid)


async def test_project_monthly_cap_blocks_paid_reservation():
    now = datetime.now(UTC).replace(tzinfo=None)
    last_month = datetime.combine(month_start_utc(), datetime.min.time()) - timedelta(days=1)
    async with get_session() as s:
        s.add(UsageLogRow(project_id=1, provider="x", status="ok", cost_usd=0.9, created_at=now))
        # last month's spend must NOT count
        s.add(UsageLogRow(project_id=1, provider="x", status="ok", cost_usd=50.0,
                          created_at=last_month))
    key = await _add_key()
    await reserve_cost(api_key=key, project=_project(monthly=1.0), estimated_cost=0.05)
    with pytest.raises(CostGuardError) as ei:
        await reserve_cost(api_key=key, project=_project(monthly=1.0), estimated_cost=0.2)
    assert ei.value.kind == "project"
    assert ei.value.limit == 1.0
    assert ei.value.used == pytest.approx(0.9)


async def test_key_monthly_cap_blocks_reservation_and_counts_up():
    key = await _add_key(monthly_cost_cap_usd=2.0, monthly_cost_used_usd=1.9)
    await reserve_cost(api_key=key, project=_project(), estimated_cost=0.05)
    assert (await _read(key.id)).monthly_cost_used_usd == pytest.approx(1.95)
    with pytest.raises(CostGuardError) as ei:
        await reserve_cost(api_key=key, project=_project(), estimated_cost=0.2)
    assert ei.value.kind == "api_key"
    assert ei.value.limit == 2.0
    assert (await _read(key.id)).monthly_cost_used_usd == pytest.approx(1.95)


async def test_key_monthly_cap_excludes_key_from_pick():
    """The pick's WHERE fragment (shared constant) drops a key at its monthly cap;
    cap 0 excludes any tier (like daily); a stale month reads 0."""
    over = await _add_key(monthly_cost_cap_usd=1.0, monthly_cost_used_usd=1.0)
    under = await _add_key(monthly_cost_cap_usd=1.0, monthly_cost_used_usd=0.5)
    uncapped = await _add_key()
    free0 = await _add_key(tier="free", monthly_cost_cap_usd=0.0)
    stale = await _add_key(monthly_cost_cap_usd=1.0, monthly_cost_used_usd=99.0,
                           monthly_reset_at=month_start_utc() - timedelta(days=31))
    never = await _add_key(monthly_cost_cap_usd=1.0, monthly_cost_used_usd=99.0,
                           monthly_reset_at=None)
    async with get_session() as s:
        ids = {r[0] for r in await s.execute(
            text(f"SELECT k.id FROM api_keys k WHERE {MONTHLY_CAP_OK_SQL}"), month_params())}
    assert over.id not in ids
    assert free0.id not in ids
    assert {under.id, uncapped.id, stale.id, never.id} <= ids


async def test_monthly_cap_zero_admits_free_tier_and_registry_free():
    paid = await _add_key(monthly_cost_cap_usd=0.0)
    with pytest.raises(CostGuardError) as ei:     # unpriced / paid model: refused
        await reserve_cost(api_key=paid, project=_project(), estimated_cost=0.0)
    assert ei.value.kind == "api_key"
    with pytest.raises(CostGuardError) as ei:
        await reserve_cost(api_key=await _add_key(), project=_project(monthly=0.0),
                           estimated_cost=5.0)
    assert ei.value.kind == "project"
    # registry-free model on the same paid key / project cap 0: admitted
    await reserve_cost(api_key=paid, project=_project(), estimated_cost=0.0,
                       known_free_model=True)
    await reserve_cost(api_key=await _add_key(), project=_project(monthly=0.0),
                       estimated_cost=0.0, known_free_model=True)
    # free-tier key: admitted (no-op)
    free = await _add_key(tier="free", monthly_cost_cap_usd=0.0)
    await reserve_cost(api_key=free, project=_project(monthly=0.0), estimated_cost=0.0)


async def test_monthly_used_resets_at_month_boundary():
    key = await _add_key(monthly_cost_cap_usd=1.0, monthly_cost_used_usd=999.0,
                         monthly_reset_at=month_start_utc() - timedelta(days=31))
    await reserve_cost(api_key=key, project=_project(), estimated_cost=0.5)
    row = await _read(key.id)
    assert row.monthly_cost_used_usd == pytest.approx(0.5)
    assert row.monthly_reset_at == month_start_utc()
    # same month, over the cap now: blocked
    with pytest.raises(CostGuardError):
        await reserve_cost(api_key=key, project=_project(), estimated_cost=0.6)


async def test_usage_booking_increments_monthly_used():
    stale = await _add_key(monthly_cost_used_usd=99.0,
                           monthly_reset_at=month_start_utc() - timedelta(days=40))
    live = await _add_key(monthly_cost_used_usd=1.0)
    for kid in (stale.id, live.id):
        await record_usage(
            api_key_id=kid, project_id=None, lease_id=None, provider="x", model="m",
            capability="chat:fast", workflow="t", tokens_in=1, tokens_out=1,
            cost_usd=0.25, latency_ms=1, status="ok", error_kind=None, http_status=200)
    assert (await _read(stale.id)).monthly_cost_used_usd == pytest.approx(0.25)
    assert (await _read(live.id)).monthly_cost_used_usd == pytest.approx(1.25)
    assert (await _read(live.id)).monthly_reset_at == month_start_utc()


async def test_release_without_any_cap_is_a_noop():
    k = ApiKeyRow(id=999_990, provider="x", label="x", tier="paid", token_encrypted="x",
                  scopes=["llm:chat"], daily_cost_cap_usd=None, monthly_cost_cap_usd=None)
    await release_cost(api_key=k, estimated_cost=5.0)


@pytest.mark.skipif(not ON_POSTGRES, reason="release uses GREATEST (Postgres)")
async def test_release_refunds_monthly_reservation():
    key = await _add_key(monthly_cost_cap_usd=2.0)
    await reserve_cost(api_key=key, project=_project(), estimated_cost=0.5)
    await release_cost(api_key=key, estimated_cost=0.5)
    assert (await _read(key.id)).monthly_cost_used_usd == pytest.approx(0.0)
