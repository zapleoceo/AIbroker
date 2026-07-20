"""Dashboard data layer — read-only aggregate queries over usage_log and
the api_keys/projects tables, plus the date-range and latency-bucket
helpers they depend on. No HTML here; callers in `dashboard.py` (routes)
and `dashboard` render turn these dicts into markup.
"""
from __future__ import annotations

import asyncio
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


async def _fetch_calls_1h() -> int:
    # Postgres-only `now()` — exercised by the Postgres-only integration tests
    # (test_gather_data_*), not the SQLite coverage run — hence the pragma.
    async with get_session() as s:  # pragma: no cover
        return int((await s.execute(text(
            "SELECT COUNT(*) FROM usage_log "
            "WHERE created_at > now() - interval '1 hour'"
        ))).scalar() or 0)


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


async def _fetch_provider_summary() -> list[Any]:
    # Postgres-only `now()`/`FILTER` — same pragma rationale as _fetch_calls_1h.
    # err_1h surfaces the 429/error storm per provider (was invisible without
    # digging the logs) — LEFT JOIN of last-hour non-ok calls, grouped by
    # provider.
    async with get_session() as s:  # pragma: no cover
        return list((await s.execute(text(
            "SELECT k.provider, "
            "COUNT(*) FILTER (WHERE is_active AND is_alive "
            "                  AND (cooldown_until IS NULL OR cooldown_until < now())) AS alive, "
            "COUNT(*) FILTER (WHERE NOT is_alive OR NOT is_active) AS dead, "
            "COUNT(*) AS total, "
            "COALESCE(e.err_1h, 0) AS err_1h "
            "FROM api_keys k "
            "LEFT JOIN ("
            "  SELECT provider, COUNT(*) AS err_1h FROM usage_log "
            "  WHERE status <> 'ok' AND created_at > now() - INTERVAL '1 hour' "
            "  GROUP BY provider"
            ") e ON e.provider = k.provider "
            "GROUP BY k.provider, e.err_1h ORDER BY k.provider"
        ))).all())


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


async def _gather_data(date_from: date | None = None,
                        date_to: date | None = None,
                        tz: ZoneInfo = UTC_TZ) -> dict[str, Any]:
    # all-time when both None; date-clamped only when at least one is provided.
    # _gather_data orchestrates Postgres-only fetches (now()/FILTER) → Postgres
    # integration suite, not the SQLite gate; _range_where itself is unit-tested.
    where_clause, bind_ = _range_where(date_from, date_to, tz)  # pragma: no cover

    # Six independent queries — none depends on another's result — so they run
    # concurrently, each on its own pooled connection (pool_size=10 +
    # max_overflow=20 comfortably covers this). A single AsyncSession can't run
    # concurrent statements, hence one get_session() per fetch, gathered here.
    (
        projects, keys, (range_totals, proj_spend),
        calls_1h, tokens_today, provider_summary,
    ) = await asyncio.gather(
        _fetch_projects(),
        _fetch_keys(),
        _fetch_range_and_proj_spend(where_clause, bind_),
        _fetch_calls_1h(),
        _fetch_tokens_today(tz),
        _fetch_provider_summary(),
    )
    return {
        "projects": projects, "keys": keys,
        "date_from": date_from, "date_to": date_to,
        "range_spend": float(range_totals["spend"]),
        "range_calls": int(range_totals["calls"]),
        "range_tin": int(range_totals["tin"]),
        "range_tout": int(range_totals["tout"]),
        "proj_spend": proj_spend,
        "tokens_today": tokens_today,
        "calls_1h": calls_1h,
        "provider_summary": provider_summary,
    }


_RANGE_HOURS = {
    "1h": 1, "4h": 4, "12h": 12,
    "24h": 24, "7d": 24 * 7, "30d": 24 * 30,
}

# Latency histogram: fixed edges (ms). width_bucket(x, edges) → 0..len(edges),
# giving len(edges)+1 buckets that must line up 1:1 with the labels below.
_LAT_EDGES_MS = (250, 500, 1000, 2000, 5000, 10000, 30000)
_LAT_LABELS = ("<250ms", "250-500ms", "0.5-1s", "1-2s",
               "2-5s", "5-10s", "10-30s", ">30s")
_LAT_SQL_ARRAY = "ARRAY[" + ",".join(str(e) for e in _LAT_EDGES_MS) + "]"


def _lat_hist_counts(rows: list[Any]) -> list[int]:
    """Map sparse (bucket, count) rows from width_bucket into a dense per-label
    count list (empty buckets → 0)."""
    counts = [0] * len(_LAT_LABELS)
    for r in rows:
        b = int(r.b)
        if 0 <= b < len(counts):
            counts[b] = int(r.n)
    return counts


_SPARK_BUCKETS = 24  # 24h default range → 1 bucket/hour; other ranges just rescale


async def _fetch_type_sparklines(  # pragma: no cover — Postgres-only, see below
    project_id: int, hours: int, column: str, n: int = _SPARK_BUCKETS
) -> dict[str, list[tuple[int, int]]]:
    """(ok, err) call counts per N equal-width time buckets spanning the
    selected range, one series per distinct `column` value ('capability' or
    'workflow') — powers the per-row mini histogram in the drill-down's
    capability/workflow card. `column` is one of two hardcoded literals
    (never caller input), so it's safe to interpolate into the SQL directly.

    Uses Postgres-only now()/width_bucket/extract(epoch) — exercised by the
    Postgres-only test_fetch_type_sparklines_splits_ok_and_error_by_bucket,
    not the SQLite diff-cover run, hence the pragma above."""
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT COALESCE({column},'(none)') AS k, "
            "  width_bucket(extract(epoch from created_at), "
            "    extract(epoch from now() - (:h * interval '1 hour')), "
            "    extract(epoch from now()), :n) AS bucket, "
            "  COUNT(*) FILTER (WHERE status='ok') AS ok_n, "
            "  COUNT(*) FILTER (WHERE status<>'ok') AS err_n "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY k, bucket"
        ), {"pid": project_id, "h": hours, "n": n})).all()
    out: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        # width_bucket is 1-indexed over [low, high); clamp the rare edge
        # (float rounding can land exactly on a boundary) into range instead
        # of silently dropping that bucket's counts.
        b = max(0, min(n - 1, int(r.bucket) - 1))
        series = out.setdefault(r.k, [(0, 0)] * n)
        ok, err = series[b]
        series[b] = (ok + int(r.ok_n), err + int(r.err_n))
    return out


async def _gather_project_detail(project_id: int, hours: int) -> dict[str, Any] | None:
    """Pull aggregates + recent calls for one project over the last `hours`."""
    async with get_session() as s:
        project = await s.get(ProjectRow, project_id)
        if not project:
            return None
        bind_ = {"pid": project_id, "h": hours}
        totals = (await s.execute(text(
            "SELECT COUNT(*) AS calls, "
            "       COALESCE(SUM(cost_usd),0) AS spend, "
            "       COALESCE(SUM(tokens_in),0) AS tin, "
            "       COALESCE(SUM(tokens_out),0) AS tout, "
            "       COALESCE(SUM(cache_read_tokens),0) AS cache_read, "
            "       COALESCE(SUM(cache_write_tokens),0) AS cache_write, "
            "       COALESCE(AVG(latency_ms),0) AS avg_lat, "
            "       COUNT(*) FILTER (WHERE status='ok') AS ok_n, "
            "       COUNT(*) FILTER (WHERE status<>'ok') AS err_n "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour')"
        ), bind_)).one()
        by_provider = (await s.execute(text(
            "SELECT provider, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS spend "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY provider ORDER BY n DESC"
        ), bind_)).all()
        by_model = (await s.execute(text(
            "SELECT model, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS spend, "
            "       COALESCE(SUM(tokens_in+tokens_out),0) AS toks, "
            "       COALESCE(SUM(tokens_in),0) AS tin, "
            "       COALESCE(SUM(cache_read_tokens),0) AS cache_r "
            "FROM usage_log WHERE project_id=:pid AND model IS NOT NULL "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY model ORDER BY n DESC LIMIT 12"
        ), bind_)).all()
        by_capability = (await s.execute(text(
            "SELECT COALESCE(capability,'(none)') AS cap, COUNT(*) AS n, "
            "       COALESCE(SUM(cost_usd),0) AS spend "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY cap ORDER BY n DESC"
        ), bind_)).all()
        by_workflow = (await s.execute(text(
            "SELECT COALESCE(workflow,'(none)') AS wf, COUNT(*) AS n, "
            "       COALESCE(SUM(cost_usd),0) AS spend "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY wf ORDER BY spend DESC, n DESC"
        ), bind_)).all()
        lat_hist_rows = (await s.execute(text(
            f"SELECT width_bucket(latency_ms, {_LAT_SQL_ARRAY}) AS b, "
            "       COUNT(*) AS n "
            "FROM usage_log WHERE project_id=:pid AND latency_ms IS NOT NULL "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY b ORDER BY b"
        ), bind_)).all()
        recent = (await s.execute(text(
            "SELECT u.id, u.created_at, u.provider, u.model, u.capability, "
            "       u.tokens_in, u.tokens_out, u.cost_usd, u.latency_ms, u.status, "
            "       u.http_status, u.error_kind, k.label AS key_label "
            "FROM usage_log u LEFT JOIN api_keys k ON k.id = u.api_key_id "
            "WHERE u.project_id=:pid "
            "ORDER BY u.created_at DESC LIMIT 50"
        ), {"pid": project_id})).all()
        # Active key count by scope intersection (informational)
    # Each opens its own pooled connection and is independent of the block
    # above and of each other — run concurrently rather than sequentially.
    cap_spark, wf_spark = await asyncio.gather(
        _fetch_type_sparklines(project_id, hours, "capability"),
        _fetch_type_sparklines(project_id, hours, "workflow"),
    )
    return {
        "project": project,
        "hours": hours,
        "totals": totals,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_capability": by_capability,
        "by_workflow": by_workflow,
        "cap_spark": cap_spark,
        "wf_spark": wf_spark,
        "lat_hist": _lat_hist_counts(list(lat_hist_rows)),
        "recent": recent,
    }
