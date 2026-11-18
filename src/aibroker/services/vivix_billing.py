"""Broker-key budgets and exactly-once usage booking for persistent Vivix holds."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.config import get_settings
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.db.vivix_models import VivixJobRow
from aibroker.providers.gemini_video import VideoRejected
from aibroker.services.vivix_store import Snapshot, VivixStore


async def held(session: AsyncSession, *, key_id: int | None = None,
               project_id: int | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(VivixJobRow.reserved_usd), 0)).where(
        VivixJobRow.api_key_id.is_not(None))
    if key_id is not None:
        stmt = stmt.where(VivixJobRow.api_key_id == key_id)
    if project_id is not None:
        stmt = stmt.where(VivixJobRow.project_id == str(project_id))
    return Decimal(str(await session.scalar(stmt)))


async def spent(session: AsyncSession, since: datetime, *, project_id: int | None = None) -> Decimal:
    stmt = select(func.coalesce(func.sum(UsageLogRow.cost_usd), 0)).where(
        UsageLogRow.created_at >= since)
    if project_id is not None:
        stmt = stmt.where(UsageLogRow.project_id == project_id)
    return Decimal(str(await session.scalar(stmt)))


def check(cap: float | None, used: Decimal, reserve: Decimal, window: str) -> None:
    if cap is not None and used + reserve > Decimal(str(cap)):
        raise VideoRejected(f"Vivix {window} budget exhausted")


class BrokerVivixStore(VivixStore):
    async def before_create(self, session: AsyncSession, project_id: str, account_id: str,
                            body: dict[str, Any], amount: Decimal) -> None:
        key_id = body.get("broker_key_id")
        if type(key_id) is not int:
            raise VideoRejected("Vivix Broker key binding is required")
        key = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id).with_for_update())
        project = await session.get(ProjectRow, int(project_id))
        if (key is None or key.provider != "vivix" or key.tier != "paid"
                or not key.is_active or not key.is_alive or "llm:video" not in key.scopes
                or key.account_id != account_id or project is None or not project.is_active
                or "llm:video" not in project.allowed_scopes):
            raise VideoRejected("Vivix Broker account/project binding is not eligible")
        now = datetime.now(UTC).replace(tzinfo=None)
        day, month = now.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        hold = await held(session, key_id=key_id)
        from aibroker.routing.budget_ledger import ordinary_held, uncounted_key_holds
        daily = key.daily_cost_used_usd if key.daily_reset_at == day.date() else 0
        monthly = key.monthly_cost_used_usd if key.monthly_reset_at == month.date() else 0
        check(key.daily_cost_cap_usd, Decimal(str(daily)) + hold + await uncounted_key_holds(
            session, key, window="daily", today=day.date()), amount, "key daily")
        check(key.monthly_cost_cap_usd, Decimal(str(monthly)) + hold + await uncounted_key_holds(
            session, key, window="monthly", today=day.date()), amount, "key monthly")
        hold = await held(session, project_id=project.id)
        hold += await ordinary_held(session, project_id=project.id)
        check(project.daily_cost_cap_usd, await spent(session, day, project_id=project.id) + hold,
              amount, "project daily")
        check(project.monthly_cost_cap_usd, await spent(session, month, project_id=project.id) + hold,
              amount, "project monthly")
        cap = get_settings().GLOBAL_DAILY_CAP_USD
        if cap > 0:
            check(cap, await spent(session, day) + await held(session) + await ordinary_held(session),
                  amount, "global daily")

    async def after_settle(self, session: AsyncSession, old: Snapshot,
                           body: dict[str, Any], amount: Decimal) -> None:
        key_id = old.body["broker_key_id"]
        key = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == key_id).with_for_update())
        if key is None or key.provider != "vivix" or key.account_id != old.account_id:
            raise VideoRejected("Vivix billing key binding changed; hold retained")
        now = datetime.now(UTC).replace(tzinfo=None)
        day, month = now.date(), now.date().replace(day=1)
        cost = float(amount)
        key.daily_cost_used_usd = (key.daily_cost_used_usd if key.daily_reset_at == day else 0) + cost
        key.monthly_cost_used_usd = (
            key.monthly_cost_used_usd if key.monthly_reset_at == month else 0) + cost
        key.daily_used = (key.daily_used if key.daily_reset_at == day else 0) + 1
        key.daily_reset_at, key.monthly_reset_at = day, month
        key.total_cost_usd += cost
        usage = UsageLogRow(api_key_id=key_id, project_id=int(old.project_id), provider="vivix",
                            model=old.body["model"], capability="video",
                            workflow=old.body.get("workflow"), request_id=f"job-{old.id}",
                            cost_usd=cost, status="ok" if body.get("provider_status") == "succeeded"
                            else "error", created_at=now)
        session.add(usage)
        await session.flush()
        body = {**body, "usage_id": usage.id}
        await session.execute(update(VivixJobRow).where(VivixJobRow.id == old.id).values(body=body))
        from aibroker.routing.cost_guard import invalidate_global_cache
        invalidate_global_cache()
