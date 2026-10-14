"""Dashboard data layer — read-only aggregate queries over usage_log and
the api_keys/projects tables, plus the date-range and latency-bucket
helpers they depend on. No HTML here; callers in `dashboard.py` (routes)
and `dashboard` render turn these dicts into markup.
"""
from __future__ import annotations

from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select, text

from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.routes.dashboard_time import UTC_TZ, day_bounds_utc, today_in


def _parse_date_range(
    date_from: str | None, date_to: str | None, tz: ZoneInfo = UTC_TZ
) -> tuple[date | None, date | None]:
    """Parse `from` and `to` strings (YYYY-MM-DD).

    Returns `(None, None)` when both inputs are missing — caller treats that
    as 'all-time, no date filter'. If only one side is given, the other
    defaults to today. Inverted ranges are swapped. Garbage strings fall back
    to today on that side (so a typo doesn't widen the window unexpectedly).
    """
    if not date_from and not date_to:
        return None, None
    today = today_in(tz)
    try:
        df = date.fromisoformat(date_from) if date_from else today
    except ValueError:
        df = today
    try:
        dt = date.fromisoformat(date_to) if date_to else today
    except ValueError:
        dt = today
    if dt < df:
        df, dt = dt, df
    return df, dt


def _range_where(
    date_from: date | None, date_to: date | None, tz: ZoneInfo = UTC_TZ
) -> tuple[str, dict[str, Any]]:
    """Sargable half-open bounds on the bare `created_at` column — no
    `created_at::date` cast, so a plain btree index on `created_at` (migration
    005) can actually be used instead of a forced full scan. Bounds are the
    selected local dates' midnights in `tz`, expressed in UTC, so the window is
    the viewer's calendar days, not the server's."""
    if date_from is None and date_to is None:
        return "", {}
    start = day_bounds_utc(date_from, tz)[0] if date_from else None
    end = day_bounds_utc(date_to, tz)[1] if date_to else None
    if start is not None and end is not None:
        return "WHERE created_at >= :start AND created_at < :end", {"start": start, "end": end}
    if start is not None:
        return "WHERE created_at >= :start", {"start": start}
    return "WHERE created_at < :end", {"end": end}


async def _fetch_range_and_proj_spend(
    where_clause: str, bind_: dict[str, Any]
) -> tuple[dict[str, Any], dict[int, float]]:
    """ONE scan (GROUP BY project_id) replaces what used to be two separate
    full-table SUMs — the all-time/range grand total is the trivial in-Python
    sum of the handful of per-project rows (a few projects, not 451k log rows),
    and the per-project spend dict comes from the same rows."""
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT project_id, COUNT(*) AS calls, "
            f"       COALESCE(SUM(cost_usd),0) AS spend, "
            f"       COALESCE(SUM(tokens_in),0) AS tin, "
            f"       COALESCE(SUM(tokens_out),0) AS tout "
            f"FROM usage_log {where_clause} GROUP BY project_id"
        ), bind_)).all()
    totals = {
        "calls": sum(int(r.calls) for r in rows),
        "spend": sum(float(r.spend) for r in rows),
        "tin": sum(int(r.tin) for r in rows),
        "tout": sum(int(r.tout) for r in rows),
    }
    proj_spend = {int(r.project_id): float(r.spend) for r in rows if r.project_id is not None}
    return totals, proj_spend


async def _fetch_tokens_today(tz: ZoneInfo = UTC_TZ) -> dict[int, dict[str, int]]:
    """Per-key token consumption for the viewer's current day — drives the
    daily-quota bar. Split in/out so manual-override caps (e.g. corp Gemini 3M in
    / 80k out) can be tracked on each axis independently. Sargable bounds
    (computed in Python, not `created_at::date =`) so the created_at index
    applies; the day is the viewer's, per `tz`."""
    start, end = day_bounds_utc(today_in(tz), tz)
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT api_key_id, "
            "  COALESCE(SUM(tokens_in + tokens_out), 0) AS tot, "
            "  COALESCE(SUM(tokens_in), 0)  AS tin, "
            "  COALESCE(SUM(tokens_out), 0) AS tout "
            "FROM usage_log "
            "WHERE api_key_id IS NOT NULL "
            "  AND created_at >= :start AND created_at < :end "
            "GROUP BY api_key_id"
        ), {"start": start, "end": end})).all()
    return {
        r.api_key_id: {"tot": int(r.tot), "tin": int(r.tin), "tout": int(r.tout)}
        for r in rows
    }


async def _fetch_projects() -> list[ProjectRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(ProjectRow).order_by(ProjectRow.id)
        )).scalars().all())


async def _fetch_keys() -> list[ApiKeyRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(ApiKeyRow).order_by(ApiKeyRow.provider, ApiKeyRow.id)
        )).scalars().all())


_RANGE_HOURS = {
    "1h": 1, "4h": 4, "12h": 12,
    "24h": 24, "7d": 24 * 7, "30d": 24 * 30,
}

# Latency histogram: fixed edges (ms). width_bucket(x, edges) → 0..len(edges),
# giving len(edges)+1 buckets that must line up 1:1 with the labels below.
_LAT_EDGES_MS = (250, 500, 1000, 2000, 5000, 10000, 30000)
_LAT_LABELS = ("<250ms", "250-500ms", "0.5-1s", "1-2s",
               "2-5s", "5-10s", "10-30s", ">30s")
