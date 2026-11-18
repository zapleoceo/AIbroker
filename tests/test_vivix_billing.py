"""Atomic usage booking and persistent reservations across Broker budget windows."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.providers.gemini_video import VideoRejected
from aibroker.routing.cost_guard import CostGuardError, reserve_cost
from aibroker.services.vivix_billing import BrokerVivixStore
from aibroker.services.vivix_service import VivixService
from tests.test_vivix_service import ledger as ledger
from tests.test_vivix_service import prepare, transport


@pytest.fixture
async def binding(ledger, monkeypatch):
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 100)
    async with get_session() as session:
        session.add(ProjectRow(id=1, name="vivix-fixture", project_key_hash="h", project_key_prefix="p",
                               allowed_scopes=["llm:video"], daily_cost_cap_usd=10,
                               monthly_cost_cap_usd=10, is_active=True))
        session.add(ApiKeyRow(id=1, provider="vivix", label="fixture", tier="paid",
                              token_encrypted="fixture-not-a-secret", account_id="account",
                              scopes=["llm:video"], daily_cost_cap_usd=10, monthly_cost_cap_usd=10,
                              is_active=True, is_alive=True))
    return VivixService(BrokerVivixStore(), transport([]))


async def prepared(service, job_id="1"):
    return await prepare(service, job_id, project_id="1", broker_key_id=1)


async def terminal(service):
    await prepared(service)
    for _ in range(2):
        await service.advance(job_id="1", project_id="1", account_id="account", api_key="FAKE")


async def test_book_usage_and_counters_once(binding):
    await terminal(binding)
    for _ in range(2):
        await binding.reconcile(job_id="1", project_id="1", actual_usd="1.25", evidence="fixture-receipt")
    async with get_session() as session:
        rows = (await session.scalars(select(UsageLogRow))).all()
        key = await session.get(ApiKeyRow, 1)
    assert len(rows) == 1 and rows[0].provider == "vivix" and rows[0].cost_usd == 1.25
    assert key.daily_cost_used_usd == key.monthly_cost_used_usd == key.total_cost_usd == 1.25
    assert (await binding.store.load("1", "1")).body["usage_id"] == rows[0].id


async def test_failed_usage_write_rolls_back_settlement_and_hold(binding, monkeypatch):
    await terminal(binding)

    async def fail(*args):
        raise OSError("fixture DB failure")

    monkeypatch.setattr(binding.store, "after_settle", fail)
    with pytest.raises(OSError):
        await binding.reconcile(job_id="1", project_id="1", actual_usd="1", evidence="fixture")
    row = await binding.store.load("1", "1")
    assert row.body["state"] == "terminal" and Decimal(row.reserved_usd) == Decimal("2.25")
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0


@pytest.mark.parametrize("target,field", [(ApiKeyRow, "daily_cost_cap_usd"),
                                        (ApiKeyRow, "monthly_cost_cap_usd"),
                                        (ProjectRow, "daily_cost_cap_usd"),
                                        (ProjectRow, "monthly_cost_cap_usd")])
async def test_broker_caps_cannot_be_bypassed_by_large_lifetime_limit(binding, target, field):
    async with get_session() as session:
        row = await session.get(target, 1)
        setattr(row, field, 2)
    with pytest.raises(VideoRejected, match="budget"):
        await prepared(binding)


async def test_project_usage_is_included_in_admission(binding):
    async with get_session() as session:
        session.add(UsageLogRow(project_id=1, provider="other", cost_usd=9, status="ok"))
    with pytest.raises(VideoRejected, match="project daily"):
        await prepared(binding)


async def test_global_cap_includes_other_projects(binding, monkeypatch):
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 3)
    async with get_session() as session:
        session.add(UsageLogRow(provider="other", cost_usd=1, status="ok"))
    with pytest.raises(VideoRejected, match="global"):
        await prepared(binding)


async def test_normal_cost_guard_sees_vivix_hold(binding):
    await prepared(binding)
    async with get_session() as session:
        key, project = await session.get(ApiKeyRow, 1), await session.get(ProjectRow, 1)
    with pytest.raises(CostGuardError):
        await reserve_cost(api_key=key, project=project, estimated_cost=8)


async def test_old_window_hold_is_not_lost_and_refund_never_subtracts_new_spend(binding):
    await terminal(binding)
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 1)
        key.daily_reset_at = datetime.now(UTC).date() - timedelta(days=40)
        key.monthly_reset_at = key.daily_reset_at.replace(day=1)
        key.daily_cost_used_usd = key.monthly_cost_used_usd = 99
    with pytest.raises(VideoRejected, match="budget"):
        # Four unresolved 2.25 ceilings fit; a fifth does not, despite stale key counters.
        for index in range(2, 6):
            await prepared(binding, str(index))
    await binding.reconcile(job_id="1", project_id="1", actual_usd="1", evidence="fixture-receipt")
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 1)
    assert key.daily_cost_used_usd == key.monthly_cost_used_usd == 1


async def test_account_binding_mismatch_fails_before_transport(binding):
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 1)
        key.account_id = "other"
    with pytest.raises(VideoRejected, match="binding"):
        await prepared(binding)


@pytest.mark.parametrize("dimension", ["project daily", "project monthly", "global daily"])
async def test_normal_cost_guard_project_and_global_see_holds(binding, monkeypatch, dimension):
    async with get_session() as session:
        key, project = await session.get(ApiKeyRow, 1), await session.get(ProjectRow, 1)
        key.daily_cost_cap_usd = key.monthly_cost_cap_usd = None
        project.daily_cost_cap_usd = 3 if dimension == "project daily" else None
        project.monthly_cost_cap_usd = 3 if dimension == "project monthly" else None
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 3 if dimension == "global daily" else 100)
    await prepared(binding)
    with pytest.raises(CostGuardError):
        await reserve_cost(api_key=key, project=project, estimated_cost=1)


async def test_settlement_refuses_changed_account_binding(binding):
    await terminal(binding)
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 1)
        key.account_id = "different-workspace"
    with pytest.raises(VideoRejected, match="binding changed"):
        await binding.reconcile(job_id="1", project_id="1", actual_usd="1", evidence="fixture")
    assert Decimal((await binding.store.load("1", "1")).reserved_usd) == Decimal("2.25")
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0
