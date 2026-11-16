"""Shared transaction lock and durable ordinary-attempt reservation lifecycle."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db.budget_models import BudgetLedgerLock, BudgetReservationRow
from aibroker.db.models import ApiKeyRow


async def lock_budget(session: AsyncSession) -> None:
    # Schema must already exist. Seed is idempotent and also supports create_all tests.
    await session.execute(text("INSERT INTO budget_ledger_lock(id, version) VALUES (1, 0) "
                               "ON CONFLICT (id) DO NOTHING"))
    await session.execute(update(BudgetLedgerLock).where(BudgetLedgerLock.id == 1)
                          .values(version=BudgetLedgerLock.version + 1))


async def ordinary_held(session: AsyncSession, *, key_id: int | None = None,
                        project_id: int | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(BudgetReservationRow.amount), 0)).where(
        BudgetReservationRow.state == "held")
    if key_id is not None:
        stmt = stmt.where(BudgetReservationRow.api_key_id == key_id)
    if project_id is not None:
        stmt = stmt.where(BudgetReservationRow.project_id == project_id)
    return Decimal(str(await session.scalar(stmt)))


async def uncounted_key_holds(session: AsyncSession, key: ApiKeyRow, *,
                              window: str, today: date) -> Decimal:
    """Current-window counter already includes some holds; include only the rest."""
    rows = (await session.scalars(select(BudgetReservationRow).where(
        BudgetReservationRow.api_key_id == key.id, BudgetReservationRow.state == "held"))).all()
    stamp = today if window == "daily" else today.replace(day=1)
    reset = key.daily_reset_at if window == "daily" else key.monthly_reset_at
    return sum((row.amount for row in rows if not (
        getattr(row, "debit_" + window) and reset == stamp
        and (row.day if window == "daily" else row.month) == stamp)), Decimal(0))


async def refund_debit(session: AsyncSession, row: BudgetReservationRow) -> None:
    key = await session.get(ApiKeyRow, row.api_key_id)
    if key is None:
        raise ValueError("Reservation key missing; hold retained")
    amount = float(row.amount)
    if row.debit_daily and key.daily_reset_at == row.day:
        key.daily_cost_used_usd = max(0, key.daily_cost_used_usd - amount)
    if row.debit_monthly and key.monthly_reset_at == row.month:
        key.monthly_cost_used_usd = max(0, key.monthly_cost_used_usd - amount)
    await session.flush()


async def booking_reservation(session: AsyncSession, reservation_id: str,
                              params: dict[str, Any]) -> BudgetReservationRow:
    row = await session.get(BudgetReservationRow, reservation_id)
    if row is None or (row.api_key_id, row.project_id) != (params["k"], params["p"]):
        raise ValueError("Reservation binding mismatch")
    fingerprint = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    if row.state == "settled":
        if row.fingerprint != fingerprint:
            raise ValueError("Reservation settlement conflict")
        return row
    if row.state != "held":
        raise ValueError("Cannot settle a released reservation")
    await refund_debit(session, row)
    row.fingerprint = fingerprint
    return row


def budget_day() -> date:
    return budget_now().date()


def budget_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
