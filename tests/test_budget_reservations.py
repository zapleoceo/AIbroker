"""Cross-provider durable budgets with real transactions and mocked calls only."""
import asyncio
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.providers.gemini_video import VideoRejected
from aibroker.routing.cost_guard import CostGuardError, release_cost, reserve_cost
from aibroker.routing.selector import record_usage
from aibroker.services.vivix_store import Snapshot
from tests.test_vivix_billing import binding as _binding_fixture
from tests.test_vivix_billing import prepared
from tests.test_vivix_service import ledger as ledger
from tests.test_vivix_service import transport

# Register shared fixtures without shadowing imported names in test arguments.
binding = _binding_fixture


@pytest.fixture
async def ordinary(binding):
    async with get_session() as session:
        session.add(ApiKeyRow(id=2, provider="other", label="fixture", tier="paid",
                              token_encrypted="FAKE", scopes=["llm:video"],
                              daily_cost_cap_usd=10, monthly_cost_cap_usd=10,
                              is_active=True, is_alive=True))
    async with get_session() as session:
        return await session.get(ApiKeyRow, 2), await session.get(ProjectRow, 1)


async def reserve(ordinary):
    key, project = ordinary
    return await reserve_cost(api_key=key, project=project, estimated_cost=2)


async def book(reservation_id, *, cost=1, project_id=1):
    return await record_usage(api_key_id=2, project_id=project_id, lease_id=None,
                              provider="other", model="fixture", capability="video", workflow=None,
                              tokens_in=0, tokens_out=0, cost_usd=cost, latency_ms=None,
                              status="ok", error_kind=None, http_status=200,
                              reservation_id=reservation_id)


@pytest.mark.parametrize("window", ["daily", "monthly", "global"])
@pytest.mark.parametrize("order", ["ordinary", "vivix", "concurrent"])
async def test_cross_key_admission(ordinary, binding, monkeypatch, window, order):
    async with get_session() as session:
        project = await session.get(ProjectRow, 1)
        if window != "global":
            setattr(project, window + "_cost_cap_usd", 3)
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 3 if window == "global" else 100)
    if order == "ordinary":
        await reserve(ordinary)
        with pytest.raises(VideoRejected, match="budget exhausted"):
            await prepared(binding)
    elif order == "vivix":
        await prepared(binding)
        with pytest.raises(CostGuardError) as exc:
            await reserve(ordinary)
        assert exc.value.kind == ("global" if window == "global" else "project")
    else:
        outcomes = await asyncio.gather(reserve(ordinary), prepared(binding), return_exceptions=True)
        assert sum(isinstance(x, str | Snapshot) for x in outcomes) == 1, outcomes
        errors = [x for x in outcomes if isinstance(x, CostGuardError | VideoRejected)]
        assert len(errors) == 1, outcomes
        if isinstance(errors[0], VideoRejected):
            assert str(errors[0]) == f"Vivix {'global daily' if window == 'global' else 'project ' + window} budget exhausted"
        else:
            assert errors[0].kind == ("global" if window == "global" else "project")


async def test_finalize_and_refund_are_idempotent(ordinary):
    rid = await reserve(ordinary)
    ids = await asyncio.gather(book(rid), book(rid))
    assert ids[0] == ids[1]
    for _ in range(2):
        await release_cost(api_key=ordinary[0], reservation_id=rid)
    assert await book(rid) == ids[0]
    with pytest.raises(ValueError, match="settlement conflict"):
        await book(rid, cost=2)
    async with get_session() as session:
        row = await session.get(BudgetReservationRow, rid)
        assert row.state == "settled" and row.usage_id == ids[0]
        key = await session.get(ApiKeyRow, 2)
        assert key.daily_cost_used_usd == key.monthly_cost_used_usd == key.total_cost_usd == 1
        assert key.daily_used == 1
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1


async def test_refund_one_of_equal_amounts_on_distinct_projects(ordinary):
    first = await reserve(ordinary)
    async with get_session() as session:
        session.add(ProjectRow(id=2, name="second", project_key_hash="h2", project_key_prefix="p2",
                               allowed_scopes=["llm:video"], is_active=True))
    async with get_session() as session:
        other = await session.get(ProjectRow, 2)
    second = await reserve_cost(api_key=ordinary[0], project=other, estimated_cost=2)
    assert first != second
    for _ in range(2):
        await release_cost(api_key=ordinary[0], reservation_id=first)
    with pytest.raises(ValueError, match="released reservation"):
        await book(first)
    with pytest.raises(ValueError, match="binding mismatch"):
        await book(second)  # wrong project, leaves second hold untouched
    async with get_session() as session:
        assert (await session.get(BudgetReservationRow, second)).state == "held"
        assert (await session.get(ApiKeyRow, 2)).daily_cost_used_usd == 2
    await book(second, project_id=2)


@pytest.mark.parametrize("settle", [True, False])
async def test_rollover_old_hold_visible_and_refund_cannot_erase_new_spend(ordinary, binding, settle):
    rid = await reserve(ordinary)
    async with get_session() as session:
        row = await session.get(BudgetReservationRow, rid)
        row.day -= timedelta(days=40)
        row.month = row.day.replace(day=1)
        key = await session.get(ApiKeyRow, 2)
        key.daily_cost_used_usd = key.monthly_cost_used_usd = key.total_cost_usd = 1
        project = await session.get(ProjectRow, 1)
        project.daily_cost_cap_usd = 3
    with pytest.raises(VideoRejected, match="project daily"):
        await prepared(binding)
    if settle:
        await book(rid, cost=0.5)
    else:
        await release_cost(api_key=ordinary[0], reservation_id=rid)
    async with get_session() as session:
        key = await session.get(ApiKeyRow, 2)
        assert key.daily_cost_used_usd == key.monthly_cost_used_usd == (1.5 if settle else 1)
        assert (await session.get(BudgetReservationRow, rid)).state != "held"
    assert Decimal((await prepared(binding)).reserved_usd) == Decimal("2.25")


async def test_rollover_hold_also_protects_key_cap(ordinary):
    rid = await reserve(ordinary)
    async with get_session() as session:
        row = await session.get(BudgetReservationRow, rid)
        row.day -= timedelta(days=40)
        row.month = row.day.replace(day=1)
        key = await session.get(ApiKeyRow, 2)
        key.daily_cost_used_usd = key.monthly_cost_used_usd = 1
        key.daily_cost_cap_usd = 3
    with pytest.raises(CostGuardError) as exc:
        await reserve_cost(api_key=ordinary[0], project=ordinary[1], estimated_cost=0.5)
    assert exc.value.kind == "api_key"


async def test_pre_submit_vivix_cancel_releases_shared_capacity(ordinary, binding):
    async with get_session() as session:
        (await session.get(ProjectRow, 1)).daily_cost_cap_usd = 3
    await prepared(binding)
    with pytest.raises(CostGuardError):
        await reserve(ordinary)
    await binding.cancel(job_id="1", project_id="1")
    assert isinstance(await reserve(ordinary), str)


async def test_ambiguous_vivix_submission_and_cancel_keep_shared_hold(ordinary, binding):
    async with get_session() as session:
        (await session.get(ProjectRow, 1)).daily_cost_cap_usd = 3
    binding.transport = transport([], post="timeout")
    await prepared(binding)
    row = await binding.advance(job_id="1", project_id="1", account_id="account", api_key="FAKE")
    assert row.body["state"] == "unknown"
    await binding.cancel(job_id="1", project_id="1")
    with pytest.raises(CostGuardError):
        await reserve(ordinary)
    row = await binding.store.load("1", "1")
    assert Decimal(row.reserved_usd) == Decimal("2.25")
