"""No provider-response/usage-booking gap, including rejection and cancellation."""
import asyncio

import pytest
from sqlalchemy import func, select

from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.providers.gemini_video import VideoRejected
from aibroker.services import attempt as att
from tests.test_budget_reservations import book, reserve
from tests.test_budget_reservations import ordinary as _ordinary_fixture
from tests.test_vivix_billing import binding as _binding_fixture
from tests.test_vivix_billing import prepared
from tests.test_vivix_service import ledger as ledger

# Register shared fixtures without shadowing imported names in test arguments.
ordinary = _ordinary_fixture
binding = _binding_fixture


@pytest.mark.parametrize("reject", [False, True])
async def test_answered_call_keeps_hold_until_atomic_booking_even_if_caller_cancelled(
        ordinary, binding, monkeypatch, reject):
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = att.record_usage

    async def paused_booking(**kwargs):
        entered.set()
        await proceed.wait()
        return await original(**kwargs)

    async def call(_):
        return "answer", {"cost_usd": 2}

    monkeypatch.setattr(att, "decrypt", lambda _: "FAKE")
    monkeypatch.setattr(att, "record_usage", paused_booking)
    async with get_session() as session:
        (await session.get(ProjectRow, 1)).daily_cost_cap_usd = 3
    a = att.Attempt(key=ordinary[0], project=ordinary[1], provider="other", model="fixture",
                    capability="video", workflow=None, call=call, estimated_cost=2,
                    note_affinity=False, check=(lambda *_: att.Rejection(
                        "fixture", att.Flow.NEXT_PROVIDER, bill=True)) if reject else None)
    caller = asyncio.create_task(att.run_attempt(a))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(VideoRejected, match="project daily"):
            await prepared(binding)  # provider has returned, booking has not begun
        pending = list(att._usage_writes)
        assert len(pending) == 1
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        proceed.set()
        await asyncio.gather(*pending)
    finally:
        proceed.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
    with pytest.raises(VideoRejected, match="project daily"):
        await prepared(binding)  # actual usage now replaces hold
    async with get_session() as session:
        row = await session.get(BudgetReservationRow, a.reservation_id)
        usage = (await session.scalars(select(UsageLogRow))).one()
        assert row.state == "settled" and row.usage_id == usage.id
        assert usage.cost_usd == 2 and usage.status == ("error" if reject else "ok")
        key = await session.get(ApiKeyRow, 2)
        assert key.daily_cost_used_usd == key.monthly_cost_used_usd == key.total_cost_usd == 2
        assert key.daily_used == 1


@pytest.mark.parametrize("cancel", [False, True])
async def test_provider_error_or_cancel_releases_exact_hold(ordinary, monkeypatch, cancel):
    async def call(_):
        if cancel:
            raise asyncio.CancelledError()
        raise RuntimeError("mock provider failure")

    monkeypatch.setattr(att, "decrypt", lambda _: "FAKE")
    a = att.Attempt(key=ordinary[0], project=ordinary[1], provider="other", model="fixture",
                    capability="video", workflow=None, call=call, estimated_cost=2,
                    note_affinity=False, penalize=False)
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await att.run_attempt(a)
    else:
        assert (await att.run_attempt(a)).flow == att.Flow.NEXT_KEY
    async with get_session() as session:
        assert (await session.get(BudgetReservationRow, a.reservation_id)).state == (
            "released" if cancel else "settled")
        key = await session.get(ApiKeyRow, 2)
        assert key.daily_cost_used_usd == key.monthly_cost_used_usd == key.total_cost_usd == 0


async def test_booking_failure_rolls_back_refund_and_keeps_hold(ordinary, monkeypatch):
    from aibroker.routing import budget_ledger
    rid = await reserve(ordinary)
    original = budget_ledger.booking_reservation

    async def fail_after_refund(*args):
        await original(*args)
        raise ValueError("fixture bookkeeping failure")

    monkeypatch.setattr(budget_ledger, "booking_reservation", fail_after_refund)
    with pytest.raises(ValueError, match="bookkeeping failure"):
        await book(rid)
    async with get_session() as session:
        assert (await session.get(BudgetReservationRow, rid)).state == "held"
        assert (await session.get(ApiKeyRow, 2)).daily_cost_used_usd == 2
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0


async def test_admission_waits_for_booking_transaction(ordinary, binding, monkeypatch):
    from aibroker.routing import budget_ledger
    rid = await reserve(ordinary)
    async with get_session() as session:
        (await session.get(ProjectRow, 1)).daily_cost_cap_usd = 3
    entered, proceed = asyncio.Event(), asyncio.Event()
    original = budget_ledger.refund_debit

    async def paused_refund(*args):
        await original(*args)
        entered.set()
        await proceed.wait()

    monkeypatch.setattr(budget_ledger, "refund_debit", paused_refund)
    booking = asyncio.create_task(book(rid, cost=2))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        admission = asyncio.create_task(prepared(binding))
        proceed.set()
        await booking
        with pytest.raises(VideoRejected, match="project daily"):
            await admission
    finally:
        proceed.set()
        await asyncio.gather(booking, return_exceptions=True)
