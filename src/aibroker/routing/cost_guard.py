"""Cap enforcement: per-key, per-project, global. Run BEFORE every paid call.

If any cap would be exceeded by est_cost, raise CostGuardError.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow


class CostGuardError(Exception):
    """Raised when a cap would be exceeded by the attempted call."""

    def __init__(self, kind: str, limit: float, used: float, attempted: float,
                 window: str = "daily"):
        self.kind = kind
        # Which budget window blocked: "daily" (resets 00:00 UTC) or "monthly"
        # (resets on the 1st of the next UTC month). Drives the client retry hint.
        self.window = window
        self.limit = limit
        self.used = used
        self.attempted = attempted
        super().__init__(
            f"{kind} cap exceeded: used ${used:.4f} + attempt ${attempted:.4f} > ${limit:.4f}"
        )


# Legacy reporting helper only. Budget admission always reads live usage under lock.
_global_cache: dict[str, float] = {"value": 0.0, "fetched_at": 0.0}
_GLOBAL_TTL_S = 30


async def _global_cost_today() -> float:
    now = time.time()
    if now - _global_cache["fetched_at"] < _GLOBAL_TTL_S:
        return _global_cache["value"]
    from datetime import timedelta as _td
    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + _td(days=1)
    async with get_session() as s:
        v = (
            await s.execute(
                text(
                    "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                    "WHERE created_at >= :start AND created_at < :end"
                ),
                {"start": day_start.replace(tzinfo=None), "end": day_end.replace(tzinfo=None)},
            )
        ).scalar() or 0.0
    _global_cache["value"] = float(v)
    _global_cache["fetched_at"] = now
    return float(v)


def invalidate_global_cache() -> None:
    _global_cache["fetched_at"] = 0.0


async def reserve_cost(
    *, api_key: ApiKeyRow, project: ProjectRow, estimated_cost: float,
    known_free_model: bool = False,
) -> str | None:
    """Return a unique durable reservation ID; finalize it with record_usage.

    Only explicit cancellation/error refunds use release_cost. Holds survive
    process death and window resets; they must never be expired by retention.
    """
    import math
    if not math.isfinite(estimated_cost) or estimated_cost < 0:
        raise ValueError("Invalid reservation amount")
    if api_key.tier == "free" and estimated_cost <= 0:
        return None
    if estimated_cost > 0 or (api_key.tier == "paid" and not known_free_model):
        for obj, kind in ((api_key, "api_key"), (project, "project")):
            for window in ("daily", "monthly"):
                if getattr(obj, window + "_cost_cap_usd") == 0:
                    raise CostGuardError(kind, 0, 0, estimated_cost, window=window)
    if estimated_cost <= 0:
        return None
    from aibroker.routing.budget_admission import admit_ordinary
    return await admit_ordinary(api_key, project, estimated_cost)


async def release_cost(*, api_key: ApiKeyRow, estimated_cost: float = 0,
                       reservation_id: str | None = None) -> None:
    """Idempotent refund by identity, never by key/amount matching.

    Missing IDs cannot release another attempt's hold. Settled rows are no-ops;
    refund after a rollover never subtracts from a newer window's counter.
    """
    if reservation_id is None:
        return
    from aibroker.db.budget_models import BudgetReservationRow
    from aibroker.routing.budget_ledger import lock_budget, refund_debit
    async with get_session() as session:
        await lock_budget(session)
        row = await session.get(BudgetReservationRow, reservation_id)
        if row is None or row.api_key_id != api_key.id:
            raise ValueError("Reservation binding mismatch")
        if row.state != "held":
            return
        await refund_debit(session, row)
        row.state = "released"
