"""Cap enforcement: per-key, per-project, global. Run BEFORE every paid call.

If any cap would be exceeded by est_cost, raise CostGuardError.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import text

from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.routing.selector import (
    FRESH_DAILY_COST_SQL,
    FRESH_MONTHLY_COST_SQL,
    month_params,
    month_start_utc,
)


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


# Cache global daily spend for 30s (low precision is OK — we mostly free tier)
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
    *,
    api_key: ApiKeyRow,
    project: ProjectRow,
    estimated_cost: float,
    known_free_model: bool = False,
) -> None:
    """Admit-or-reject an attempted call against api_key/project/global caps,
    call BEFORE the provider call. Raise CostGuardError if any cap would be
    exceeded. For free-tier keys (tier='free' and cost<=0) → no-op, since
    `services/llm_service._billed_cost` forces their real cost to $0 regardless
    of estimate.

    Pair with `release_cost(api_key, estimated_cost)` once the attempt resolves
    (success or failure) — this reserves the ESTIMATE now and refunds it later,
    while `record_usage` separately books the REAL final cost. Net effect: the
    estimate only counts toward the cap for the few hundred ms the call is
    actually in flight.

    2026-07-03: the per-key check used to be a plain Python comparison against
    an `api_key` object loaded earlier in the request — two concurrent calls
    against the same key could both read the same stale `daily_cost_used_usd`
    and both pass, overshooting the cap by up to `concurrency × call_cost`. Now
    it's a single atomic `UPDATE ... WHERE ... RETURNING` (see
    `routing.selector.FRESH_DAILY_COST_SQL`): Postgres row-locks the key for the
    statement, so a second concurrent reservation against the same key waits,
    then re-evaluates its own WHERE against the first's already-committed
    value — no stale read possible. This UPDATE is also the day's first writer
    to `daily_cost_used_usd`/`daily_reset_at` most of the time, so it doubles as
    the lazy daily-reset (a "daily" cap that never reset was a confirmed prod
    bug — see selector.py's `FRESH_DAILY_COST_SQL` docstring).

    Per-project (live SUM, self-resetting daily by construction) and global
    (30s-cached SUM) checks are unchanged — smaller, documented residual races
    remain there (see docs/routing.md **Cost guard**); they're a secondary
    backstop behind the now-atomic per-key cap, which is the tighter, more
    commonly-set limit in practice.
    """
    if api_key.tier == "free" and estimated_cost <= 0:
        return

    # A cap of 0 means "free providers only": a paid key is refused unless the
    # model is registry-known free. `used + estimate > cap` alone admits it when
    # the estimate is $0, which is also what an UNPRICED model estimates — that
    # was the hole: its real cost then lands uncapped. Only tier=="paid": a
    # trial key was never refused here on master, and still is not. Checked
    # before any reservation, so nothing needs refunding.
    if api_key.tier == "paid" and not known_free_model:
        if api_key.daily_cost_cap_usd == 0:
            raise CostGuardError("api_key", 0.0, api_key.daily_cost_used_usd or 0.0,
                                 estimated_cost)
        if api_key.monthly_cost_cap_usd == 0:
            raise CostGuardError("api_key", 0.0, api_key.monthly_cost_used_usd or 0.0,
                                 estimated_cost, window="monthly")
        if project.daily_cost_cap_usd == 0:
            raise CostGuardError("project", 0.0, 0.0, estimated_cost)
        if project.monthly_cost_cap_usd == 0:
            raise CostGuardError("project", 0.0, 0.0, estimated_cost, window="monthly")

    # 1. per-key — atomic reserve-and-admit in ONE statement covering every capped
    # window (daily and/or monthly): all counters are debited together or none is.
    # The monthly window mirrors the daily one (lazy reset keyed on the UTC month).
    key_daily = api_key.daily_cost_cap_usd is not None
    key_monthly = api_key.monthly_cost_cap_usd is not None
    if key_daily or key_monthly:
        sets: list[str] = []
        conds = ["k.id = :id"]
        if key_daily:
            sets += [f"daily_cost_used_usd = {FRESH_DAILY_COST_SQL} + :c",
                     "daily_reset_at = CURRENT_DATE"]
            conds.append(f"{FRESH_DAILY_COST_SQL} + :c <= k.daily_cost_cap_usd")
        if key_monthly:
            sets += [f"monthly_cost_used_usd = {FRESH_MONTHLY_COST_SQL} + :c",
                     "monthly_reset_at = :month_start"]
            conds.append(f"{FRESH_MONTHLY_COST_SQL} + :c <= k.monthly_cost_cap_usd")
        params: dict[str, object] = {"id": api_key.id, "c": estimated_cost}
        if key_monthly:
            params.update(month_params())
        async with get_session() as s:
            row = (await s.execute(
                text(
                    f"UPDATE api_keys AS k SET {', '.join(sets)} "
                    f"WHERE {' AND '.join(conds)} RETURNING id"
                ),
                params,
            )).first()
        if row is None:
            if key_monthly and api_key.monthly_reset_at == month_start_utc() and (
                (api_key.monthly_cost_used_usd or 0.0) + estimated_cost
                > api_key.monthly_cost_cap_usd
            ):
                raise CostGuardError("api_key", api_key.monthly_cost_cap_usd,
                                     api_key.monthly_cost_used_usd, estimated_cost,
                                     window="monthly")
            raise CostGuardError(
                "api_key",
                api_key.daily_cost_cap_usd if key_daily else api_key.monthly_cost_cap_usd,
                api_key.daily_cost_used_usd if key_daily else api_key.monthly_cost_used_usd,
                estimated_cost,
                window="daily" if key_daily else "monthly",
            )

    # 2. per-project
    if project.daily_cost_cap_usd is not None:
        from datetime import timedelta as _td
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + _td(days=1)
        async with get_session() as s:
            used = (
                await s.execute(
                    text(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                        "WHERE project_id = :pid AND created_at >= :start AND created_at < :end"
                    ),
                    {
                        "pid": project.id,
                        "start": day_start.replace(tzinfo=None),
                        "end": day_end.replace(tzinfo=None),
                    },
                )
            ).scalar() or 0.0
        if used + estimated_cost > project.daily_cost_cap_usd:
            # Refund the per-key reservation we just took — this attempt never happens.
            await release_cost(api_key=api_key, estimated_cost=estimated_cost)
            raise CostGuardError(
                "project",
                project.daily_cost_cap_usd,
                float(used),
                estimated_cost,
            )

    # 2b. per-project monthly — live SUM over the UTC calendar month (self-resetting
    # by construction, exactly like the daily project check).
    if project.monthly_cost_cap_usd is not None:
        m_start = datetime.combine(month_start_utc(), datetime.min.time())
        async with get_session() as s:
            m_used = (
                await s.execute(
                    text(
                        "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                        "WHERE project_id = :pid AND created_at >= :start"
                    ),
                    {"pid": project.id, "start": m_start},
                )
            ).scalar() or 0.0
        if m_used + estimated_cost > project.monthly_cost_cap_usd:
            await release_cost(api_key=api_key, estimated_cost=estimated_cost)
            raise CostGuardError(
                "project", project.monthly_cost_cap_usd, float(m_used), estimated_cost,
                window="monthly",
            )

    # 3. global
    s = get_settings()
    if s.GLOBAL_DAILY_CAP_USD > 0:
        used = await _global_cost_today()
        if used + estimated_cost > s.GLOBAL_DAILY_CAP_USD:
            await release_cost(api_key=api_key, estimated_cost=estimated_cost)
            raise CostGuardError(
                "global", s.GLOBAL_DAILY_CAP_USD, used, estimated_cost
            )


async def release_cost(*, api_key: ApiKeyRow, estimated_cost: float) -> None:
    """Undo a `reserve_cost` reservation once the attempt resolves (success or
    failure) — `record_usage` then books the real final cost on top, so the key
    ends up debited by exactly the real cost, never the estimate.

    `GREATEST(0, ...)` guards float rounding from ever pushing the counter
    negative. Must mirror `reserve_cost`'s PER-KEY skip condition exactly —
    reserve only debits the counter when `daily_cost_cap_usd is not None`, so
    release must skip on the same condition. (It used to skip on `tier=="free"`
    instead: a paid key with NO per-key cap but a project/global cap took no
    reservation yet got refunded on a project/global block, corrupting its daily
    counter. See fix 2026-07-10.)"""
    # TODO(monthly-reset): a refund that lands after a UTC day/month boundary subtracts
    # from the NEW window's counter (GREATEST(0, ...) is not checked against the
    # daily_reset_at / monthly_reset_at stamp), so it can under-count by <= one
    # estimate; the in-flight window is milliseconds, so it was left as is.
    key_daily = api_key.daily_cost_cap_usd is not None
    key_monthly = api_key.monthly_cost_cap_usd is not None
    if not (key_daily or key_monthly) or estimated_cost <= 0:
        return
    sets = []
    if key_daily:
        sets.append("daily_cost_used_usd = GREATEST(0, daily_cost_used_usd - :c)")
    if key_monthly:
        sets.append("monthly_cost_used_usd = GREATEST(0, monthly_cost_used_usd - :c)")
    async with get_session() as s:
        await s.execute(
            text(f"UPDATE api_keys SET {', '.join(sets)} WHERE id = :id"),
            {"c": estimated_cost, "id": api_key.id},
        )
