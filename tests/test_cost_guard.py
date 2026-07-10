"""Cost guard — three-tier daily cap enforcement + atomic per-key reservation.

reserve_cost's per-key branch does a real atomic UPDATE against api_keys, so
most of this file needs a real Postgres row (SQLite can't autoincrement the
BigInteger PK the same way — matches the project-wide convention seen in
test_selector.py). The free-tier / no-cap bypass tests never touch the DB
(reserve_cost returns before any query), so they run everywhere.
"""
from __future__ import annotations

import os
from datetime import date

import pytest
from sqlalchemy import insert

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.routing.cost_guard import (
    CostGuardError,
    invalidate_global_cache,
    release_cost,
    reserve_cost,
)

_DB = os.environ.get("DATABASE_URL", "")
ON_POSTGRES = "postgres" in _DB or "asyncpg" in _DB


def _project(*, cap=None) -> ProjectRow:
    return ProjectRow(
        id=1, name="x", project_key_hash="x", project_key_prefix="x",
        allowed_scopes=["llm:chat"], daily_cost_cap_usd=cap,
        is_active=True, notes="",
    )


async def _add_key(**kw) -> ApiKeyRow:
    """Insert a real api_keys row — reserve_cost's atomic UPDATE needs a real
    row to match against. Returns an ApiKeyRow hydrated with the real id."""
    defaults = {
        "provider": "x", "label": "x", "tier": "paid",
        "scopes": ["llm:chat"], "token_encrypted": "x",
        "is_active": True, "is_alive": True,
        "daily_limit": 999999, "daily_used": 0,
        # Seeded usage represents TODAY's spend — a None/stale daily_reset_at
        # would make the reset-aware SQL read the counter as 0.
        "daily_cost_used_usd": 0.0, "daily_cost_cap_usd": None,
        "daily_reset_at": date.today(),
        "monthly_cost_used_usd": 0.0, "total_cost_usd": 0.0,
        "error_count": 0, "notes": "",
    }
    defaults.update(kw)
    async with get_session() as s:
        r = await s.execute(insert(ApiKeyRow).returning(ApiKeyRow.id), defaults)
        kid = int(r.scalar_one())
    defaults["id"] = kid
    return ApiKeyRow(**defaults)


async def _read_key(kid: int) -> ApiKeyRow:
    async with get_session() as s:
        return await s.get(ApiKeyRow, kid)


# ─── Free-tier / no-cap bypass — pure, never touches the DB ─────────────────


async def test_free_key_zero_cost_passes():
    """Free tier with cost<=0 returns before any query — safe with a fake,
    never-persisted key id."""
    k = ApiKeyRow(id=999_999, provider="x", label="x", tier="free",
                  token_encrypted="x", scopes=["llm:chat"], daily_cost_used_usd=0)
    await reserve_cost(api_key=k, project=_project(), estimated_cost=0.0)


async def test_key_with_no_cap_skips_the_db_entirely():
    """daily_cost_cap_usd=None → the per-key branch never runs — an unsaved,
    fake key id is fine since no query ever references it."""
    k = ApiKeyRow(id=999_999, provider="x", label="x", tier="paid",
                  token_encrypted="x", scopes=["llm:chat"],
                  daily_cost_used_usd=1_000_000, daily_cost_cap_usd=None)
    await reserve_cost(api_key=k, project=_project(), estimated_cost=10.0)


async def test_release_cost_free_tier_is_a_noop():
    k = ApiKeyRow(id=999_999, provider="x", label="x", tier="free",
                  token_encrypted="x", scopes=["llm:chat"])
    await release_cost(api_key=k, estimated_cost=5.0)  # no DB touch, no error


async def test_invalidate_global_cache_resets_ttl():
    invalidate_global_cache()
    invalidate_global_cache()


# ─── Atomic per-key reservation — needs a real Postgres row ─────────────────


@pytest.mark.skipif(not ON_POSTGRES, reason="reserve_cost's UPDATE needs a real row")
async def test_paid_under_cap_reserves_and_persists():
    key = await _add_key(daily_cost_used_usd=0.5, daily_cost_cap_usd=2.0)
    await reserve_cost(api_key=key, project=_project(), estimated_cost=0.1)
    row = await _read_key(key.id)
    assert row.daily_cost_used_usd == pytest.approx(0.6)


@pytest.mark.skipif(not ON_POSTGRES, reason="reserve_cost's UPDATE needs a real row")
async def test_paid_at_cap_blocks_without_reserving():
    key = await _add_key(daily_cost_used_usd=1.99, daily_cost_cap_usd=2.0)
    with pytest.raises(CostGuardError) as exc:
        await reserve_cost(api_key=key, project=_project(), estimated_cost=0.05)
    assert exc.value.kind == "api_key"
    assert exc.value.limit == 2.0
    assert exc.value.used == pytest.approx(1.99)
    assert exc.value.attempted == 0.05
    # A rejected reservation must not have touched the counter.
    row = await _read_key(key.id)
    assert row.daily_cost_used_usd == pytest.approx(1.99)


@pytest.mark.skipif(not ON_POSTGRES, reason="reserve_cost's UPDATE needs a real row")
async def test_release_cost_refunds_reservation():
    key = await _add_key(daily_cost_used_usd=0.0, daily_cost_cap_usd=2.0)
    await reserve_cost(api_key=key, project=_project(), estimated_cost=0.5)
    assert (await _read_key(key.id)).daily_cost_used_usd == pytest.approx(0.5)
    await release_cost(api_key=key, estimated_cost=0.5)
    assert (await _read_key(key.id)).daily_cost_used_usd == pytest.approx(0.0)


@pytest.mark.skipif(not ON_POSTGRES, reason="reserve_cost's UPDATE needs a real row")
async def test_release_cost_never_goes_negative():
    """GREATEST(0, ...) guard: releasing more than was ever reserved (e.g. a
    stray double-release) clamps at 0 instead of underflowing negative."""
    key = await _add_key(daily_cost_used_usd=0.1, daily_cost_cap_usd=2.0)
    await release_cost(api_key=key, estimated_cost=5.0)
    assert (await _read_key(key.id)).daily_cost_used_usd == 0.0


# ─── The actual race this change closes ──────────────────────────────────────


@pytest.mark.skipif(not ON_POSTGRES, reason="tests real concurrent Postgres UPDATEs")
async def test_concurrent_reservations_never_overshoot_cap():
    """REGRESSION: the old check_caps read a pre-loaded ApiKeyRow object with
    no lock — two requests racing the same key could both pass a stale
    comparison and both spend, overshooting the cap. reserve_cost's atomic
    UPDATE...WHERE must admit exactly as many concurrent reservations as fit
    under the cap, never more, regardless of how many race it at once."""
    import asyncio

    key = await _add_key(daily_cost_used_usd=0.0, daily_cost_cap_usd=1.0)
    project = _project()

    async def attempt() -> bool:
        try:
            await reserve_cost(api_key=key, project=project, estimated_cost=0.3)
            return True
        except CostGuardError:
            return False

    # 5 concurrent attempts at $0.30 each against a $1.00 cap — only 3 fit.
    results = await asyncio.gather(*(attempt() for _ in range(5)))
    admitted = sum(results)
    assert admitted == 3
    row = await _read_key(key.id)
    assert row.daily_cost_used_usd == pytest.approx(0.9)  # 3 × 0.3, never more


# ─── Daily reset self-healing ────────────────────────────────────────────────


@pytest.mark.skipif(not ON_POSTGRES, reason="reserve_cost's UPDATE needs a real row")
async def test_reserve_cost_resets_stale_counter():
    """REGRESSION: daily_cost_used_usd never reset — confirmed on prod, a key
    accumulated daily_used=51,921 over 6 days with daily_reset_at still NULL.
    reserve_cost must treat a non-today daily_reset_at as 0, not add the
    estimate on top of stale accumulation (which would permanently lock the
    key out the first time its 'daily' cap was ever crossed)."""
    from datetime import date, timedelta

    key = await _add_key(
        daily_cost_used_usd=999.0, daily_cost_cap_usd=1.0,
        daily_reset_at=date.today() - timedelta(days=3),
    )
    # Would raise instantly if 999 + 0.5 were compared against the $1 cap —
    # must instead treat the stale 999 as 0 and succeed.
    await reserve_cost(api_key=key, project=_project(), estimated_cost=0.5)
    row = await _read_key(key.id)
    assert row.daily_cost_used_usd == pytest.approx(0.5)
    assert row.daily_reset_at == date.today()


async def test_release_cost_paid_key_without_per_key_cap_is_a_noop():
    """REGRESSION (2026-07-10): a paid key with NO per-key cap took no
    reservation (reserve_cost skips when daily_cost_cap_usd is None), so release
    must skip too. It used to skip only on tier=='free' and would decrement such
    a key's counter on a project/global block, corrupting real spend."""
    k = ApiKeyRow(id=999_997, provider="x", label="x", tier="paid",
                  token_encrypted="x", scopes=["llm:chat"], daily_cost_cap_usd=None)
    await release_cost(api_key=k, estimated_cost=5.0)  # returns early, no DB touch


@pytest.mark.skipif(not ON_POSTGRES, reason="reserve_cost + usage_log need a real DB")
async def test_project_block_does_not_corrupt_uncapped_paid_key_counter():
    """A paid key with NO per-key cap, blocked by the PROJECT cap: reserve took
    nothing (no per-key cap), so the project-block refund must NOT decrement the
    key's daily counter. Pre-fix it wrongly dropped 1.0 → 0.95."""
    from datetime import UTC, datetime

    from aibroker.db.models import ProjectRow, UsageLogRow
    # A REAL persisted project (usage_log.project_id has an FK to projects).
    async with get_session() as s:
        pr = await s.execute(insert(ProjectRow).returning(ProjectRow.id), {
            "name": f"p-{os.urandom(3).hex()}", "project_key_hash": "x",
            "project_key_prefix": "x", "allowed_scopes": ["llm:chat"],
            "daily_cost_cap_usd": 0.01, "is_active": True, "notes": "",
        })
        pid = int(pr.scalar_one())
    proj = ProjectRow(
        id=pid, name="x", project_key_hash="x", project_key_prefix="x",
        allowed_scopes=["llm:chat"], daily_cost_cap_usd=0.01, is_active=True, notes="",
    )
    key = await _add_key(tier="paid", daily_cost_cap_usd=None, daily_cost_used_usd=1.0)
    async with get_session() as s:
        s.add(UsageLogRow(project_id=pid, provider="x", status="ok",
                          cost_usd=0.02, created_at=datetime.now(UTC).replace(tzinfo=None)))
    with pytest.raises(CostGuardError) as exc:
        await reserve_cost(api_key=key, project=proj, estimated_cost=0.05)
    assert exc.value.kind == "project"
    assert (await _read_key(key.id)).daily_cost_used_usd == pytest.approx(1.0)
