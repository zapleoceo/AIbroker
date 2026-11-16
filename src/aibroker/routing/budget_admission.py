"""Admission of ordinary attempts under the shared budget transaction lock."""
from decimal import ROUND_CEILING, Decimal
from uuid import uuid4

from sqlalchemy import select

from aibroker.config import get_settings
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.routing.budget_ledger import (
    budget_day,
    lock_budget,
    ordinary_held,
    uncounted_key_holds,
)


async def admit_ordinary(api_key: ApiKeyRow, project: ProjectRow, estimate: float) -> str:
    from aibroker.routing.cost_guard import CostGuardError
    from aibroker.services.vivix_billing import held, spent

    amount = Decimal(str(estimate))
    if not amount.is_finite() or amount <= 0 or amount >= Decimal("1000000000000000"):
        raise ValueError("Invalid reservation amount")
    amount = amount.quantize(Decimal("0.00000001"), rounding=ROUND_CEILING)
    today = budget_day()
    from datetime import datetime
    day = datetime.combine(today, datetime.min.time())
    month = day.replace(day=1)
    async with get_session() as session:
        await lock_budget(session)
        key = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == api_key.id).with_for_update())
        if key is None:
            raise ValueError("Reservation key missing")
        # Honor both fresh administrative limits and stricter caller restrictions.
        stored_project = await session.get(ProjectRow, project.id) or project

        def project_cap(window):
            caps = [getattr(obj, window + "_cost_cap_usd") for obj in (project, stored_project)]
            return min((cap for cap in caps if cap is not None), default=None)

        def check(cap, used, kind, window="daily"):
            if cap is not None and used + amount > Decimal(str(cap)):
                raise CostGuardError(kind, cap, float(used), float(amount), window=window)

        for window, stamp in (("daily", today), ("monthly", today.replace(day=1))):
            used = (getattr(key, window + "_cost_used_usd")
                    if getattr(key, window + "_reset_at") == stamp else 0)
            used = Decimal(str(used)) + await held(session, key_id=key.id)
            used += await uncounted_key_holds(session, key, window=window, today=today)
            check(getattr(key, window + "_cost_cap_usd"), used, "api_key", window)
        project_hold = await held(session, project_id=project.id)
        project_hold += await ordinary_held(session, project_id=project.id)
        check(project_cap("daily"), await spent(session, day, project_id=project.id)
              + project_hold, "project")
        check(project_cap("monthly"), await spent(session, month, project_id=project.id)
              + project_hold, "project", "monthly")
        cap = get_settings().GLOBAL_DAILY_CAP_USD
        if cap > 0:
            check(cap, await spent(session, day) + await held(session)
                  + await ordinary_held(session), "global")
        row = BudgetReservationRow(id=str(uuid4()), api_key_id=key.id, project_id=project.id,
                                   amount=amount, day=today, month=today.replace(day=1),
                                   debit_daily=key.daily_cost_cap_usd is not None,
                                   debit_monthly=key.monthly_cost_cap_usd is not None, state="held")
        for window, stamp in (("daily", row.day), ("monthly", row.month)):
            if getattr(row, "debit_" + window):
                counter = window + "_cost_used_usd"
                reset = window + "_reset_at"
                used = getattr(key, counter) if getattr(key, reset) == stamp else 0
                setattr(key, counter, used + float(amount))
                setattr(key, reset, stamp)
        session.add(row)
        await session.flush()
        return row.id
