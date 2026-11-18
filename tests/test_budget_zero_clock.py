"""Zero estimates still check budget debt; clocks are sampled after lock waits."""
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.routing import budget_admission
from aibroker.routing.cost_guard import CostGuardError, reserve_cost
from tests.test_budget_reservations import book, reserve
from tests.test_budget_reservations import ordinary as ordinary
from tests.test_vivix_billing import binding as binding
from tests.test_vivix_billing import prepared
from tests.test_vivix_service import ledger as ledger


@pytest.mark.parametrize("window", ["key_daily", "key_monthly", "project_daily", "project_monthly", "global"])
@pytest.mark.parametrize("source", ["spent", "ordinary", "vivix"])
@pytest.mark.parametrize("tier,known_free", [("paid", False), ("trial", False), ("paid", True)])
async def test_zero_estimate_rejects_exceeded_budget(ordinary, binding, monkeypatch,
                                                   window, source, tier, known_free):
    key, project = ordinary
    if source == "ordinary":
        await reserve(ordinary)
    elif source == "vivix":
        await prepared(binding)
        if window.startswith("key"):
            async with get_session() as session:
                key = await session.get(ApiKeyRow, 1)
    async with get_session() as session:
        stored_key = await session.get(ApiKeyRow, key.id)
        stored_key.tier = tier
        if source == "spent":
            today = datetime.now(UTC).date()
            stored_key.daily_reset_at = today
            stored_key.monthly_reset_at = today.replace(day=1)
            stored_key.daily_cost_used_usd = stored_key.monthly_cost_used_usd = 2
            session.add(UsageLogRow(project_id=1, provider="fixture", cost_usd=2, status="ok"))
        if window.startswith("key"):
            setattr(stored_key, window.split("_")[1] + "_cost_cap_usd", 1)
        elif window.startswith("project"):
            row = await session.get(ProjectRow, 1)
            setattr(row, window.split("_")[1] + "_cost_cap_usd", 1)
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 1 if window == "global" else 100)
    key.tier = tier
    async with get_session() as session:
        before = await session.scalar(select(func.count()).select_from(BudgetReservationRow))
    with pytest.raises(CostGuardError) as exc:
        await reserve_cost(api_key=key, project=project, estimated_cost=0, known_free_model=known_free)
    assert exc.value.kind == ("api_key" if window.startswith("key") else window.split("_")[0])
    assert exc.value.window == ("monthly" if window.endswith("monthly") else "daily")
    assert exc.value.attempted == 0 and exc.value.used > 1
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(BudgetReservationRow)) == before


async def test_free_tier_zero_keeps_explicit_bypass(ordinary, monkeypatch):
    key, project = ordinary
    key.tier = "free"
    key.daily_cost_cap_usd = project.daily_cost_cap_usd = 0
    key.daily_cost_used_usd = 99

    async def forbidden(*args):
        raise AssertionError("free-tier bypass must not touch admission")

    monkeypatch.setattr(budget_admission, "admit_ordinary", forbidden)
    assert await reserve_cost(api_key=key, project=project, estimated_cost=0) is None


@pytest.mark.parametrize("estimate", [0, 2])
async def test_admission_clock_is_sampled_after_lock_crosses_month_boundary(ordinary, monkeypatch, estimate):
    old, new = date(2026, 1, 31), date(2026, 2, 1)
    current, samples = [old], []
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 2)
        key.daily_reset_at, key.monthly_reset_at = old, old.replace(day=1)
        key.daily_cost_used_usd = key.monthly_cost_used_usd = 9
        key.daily_cost_cap_usd = key.monthly_cost_cap_usd = 3
        project = await session.get(ProjectRow, 1)
        project.daily_cost_cap_usd = project.monthly_cost_cap_usd = 3
        session.add(UsageLogRow(project_id=1, provider="fixture", cost_usd=9, status="ok",
                                created_at=datetime(2026, 1, 31, 23, 59)))
    original = budget_admission.lock_budget

    async def crossing_lock(session):
        await original(session)
        current[0] = new

    def clock():
        samples.append(current[0])
        return current[0]

    monkeypatch.setattr(budget_admission, "lock_budget", crossing_lock)
    monkeypatch.setattr(budget_admission, "budget_day", clock)
    rid = await reserve_cost(api_key=ordinary[0], project=ordinary[1], estimated_cost=estimate)
    assert samples == [new]
    async with get_session() as session:
        if estimate:
            row = await session.get(BudgetReservationRow, rid)
            assert row.day == row.month == new
            key = await session.get(ApiKeyRow, 2)
            assert key.daily_reset_at == key.monthly_reset_at == new
            assert key.daily_cost_used_usd == key.monthly_cost_used_usd == 2
        else:
            assert rid is None
            assert await session.scalar(select(func.count()).select_from(BudgetReservationRow)) == 0


async def test_zero_estimate_at_exact_cap_is_allowed_without_mutation(ordinary):
    key, project = ordinary
    async with get_session() as session:
        row = await session.get(ApiKeyRow, key.id)
        row.daily_reset_at = datetime.now(UTC).date()
        row.daily_cost_cap_usd = row.daily_cost_used_usd = 2
    assert await reserve_cost(api_key=key, project=project, estimated_cost=0) is None
    async with get_session() as session:
        assert (await session.get(ApiKeyRow, key.id)).daily_cost_used_usd == 2
        assert await session.scalar(select(func.count()).select_from(BudgetReservationRow)) == 0


@pytest.mark.parametrize("known_free", [False, True])
async def test_fresh_zero_cap_preserves_known_free_semantics(ordinary, known_free):
    key, project = ordinary  # detached caller still has cap 10
    async with get_session() as session:
        (await session.get(ApiKeyRow, key.id)).daily_cost_cap_usd = 0
    if known_free:
        assert await reserve_cost(api_key=key, project=project, estimated_cost=0,
                                  known_free_model=True) is None
    else:
        with pytest.raises(CostGuardError):
            await reserve_cost(api_key=key, project=project, estimated_cost=0)
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(BudgetReservationRow)) == 0


async def test_booking_clock_after_lock_stamps_usage_and_counters_together(ordinary, monkeypatch):
    from aibroker.routing import budget_ledger
    rid = await reserve(ordinary)
    old, new = datetime(2026, 1, 31, 23, 59), datetime(2026, 2, 1, 0, 1)
    current, samples = [old], []
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 2)
        key.daily_reset_at, key.monthly_reset_at = old.date(), old.date().replace(day=1)
        key.daily_cost_used_usd = key.monthly_cost_used_usd = 9
        row = await session.get(BudgetReservationRow, rid)
        row.day, row.month = old.date(), old.date().replace(day=1)
    original = budget_ledger.lock_budget

    async def crossing_lock(session):
        await original(session)
        current[0] = new

    def clock():
        samples.append(current[0])
        return current[0]

    monkeypatch.setattr(budget_ledger, "lock_budget", crossing_lock)
    monkeypatch.setattr(budget_ledger, "budget_now", clock)
    usage_id = await book(rid)
    assert samples == [new]
    async with get_session() as session:
        usage = await session.get(UsageLogRow, usage_id)
        assert usage.created_at.replace(tzinfo=None) == new
        key = await session.get(ApiKeyRow, 2)
        assert key.daily_reset_at == key.monthly_reset_at == new.date()
        assert key.daily_cost_used_usd == key.monthly_cost_used_usd == 1
