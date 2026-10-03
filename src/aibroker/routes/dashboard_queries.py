"""Read-only aggregate queries behind the redesigned admin pages.

Everything here is portable SQL (plain GROUP BY / CASE / window functions,
bounds computed in Python) so the same code runs on Postgres in production and
on SQLite in the test suite — no `now()`, `FILTER` or `date_trunc` outside the
one dialect switch in `_bucket_sql`. Nothing writes.

Rows come back as plain dicts with datetimes normalised to naive UTC (SQLite
returns text for raw-SQL datetime columns; Postgres returns datetimes).
"""
from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text

from aibroker.db import get_session
from aibroker.db.models import AuditLogRow
from aibroker.routes.dashboard_data import _LAT_EDGES_MS, _LAT_LABELS

REQUEST_PAGE_SIZE = 50
CSV_EXPORT_LIMIT = 20_000
AUDIT_PAGE_SIZE = 50
STUCK_PENDING_MIN = 30   # same threshold monitor.check_queue_backlog alerts on
STUCK_RUNNING_MIN = 25   # job_queue reclaims `running` jobs after this long
_ALL_TIME_DAYS = 120     # usage_log retention, the 'all' window's left edge

# Whitelisted ORDER BY — user input only ever selects a key, never SQL.
_SORTS: dict[str, str] = {
    "time": "u.id",
    "cost": "u.cost_usd",
    "latency": "COALESCE(u.latency_ms, -1)",
    "tokens": "(u.tokens_in + u.tokens_out)",
}


# ─── helpers ────────────────────────────────────────────────────────────────


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _dt(v: Any) -> datetime | None:
    if v is None or isinstance(v, datetime):
        return v
    return datetime.fromisoformat(str(v))


def _row(r: Any, *dt_keys: str) -> dict[str, Any]:
    d = dict(r._mapping)
    for k in dt_keys:
        if k in d:
            d[k] = _dt(d[k])
    return d


def _win(start: datetime | None, end: datetime | None, col: str = "created_at",
         params: dict[str, Any] | None = None) -> tuple[list[str], dict[str, Any]]:
    """Half-open [start, end) conditions on `col` (sargable: the bare column)."""
    params = {} if params is None else params
    conds: list[str] = []
    if start is not None:
        conds.append(f"{col} >= :w_start")
        params["w_start"] = start
    if end is not None:
        conds.append(f"{col} < :w_end")
        params["w_end"] = end
    return conds, params


def _where(conds: Iterable[str]) -> str:
    conds = list(conds)
    return ("WHERE " + " AND ".join(conds)) if conds else ""


async def _dialect() -> str:
    async with get_session() as s:
        return str(s.bind.dialect.name)


async def gather(*aws: Any) -> list[Any]:
    """asyncio.gather on Postgres (each query has its own pooled connection);
    sequential on SQLite, whose single shared test connection cannot serve
    contended coroutines from another event loop."""
    if await _dialect() == "sqlite":
        return [await a for a in aws]
    return list(await asyncio.gather(*aws))


def _bucket_sql(bucket: str, dialect: str, col: str = "created_at") -> str:
    """SQL expression flooring `col` to the hour/day (UTC) — the one place the
    two databases differ. `bucket` is one of two literals, never user input."""
    if bucket not in ("hour", "day"):
        raise ValueError(bucket)
    if dialect == "postgresql":  # pragma: no cover — checked against prod SELECTs
        return f"date_trunc('{bucket}', {col})"
    fmt = "%Y-%m-%d %H:00:00" if bucket == "hour" else "%Y-%m-%d 00:00:00"
    return f"strftime('{fmt}', {col})"


def floor_bucket(dt: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_starts(start: datetime, end: datetime, bucket: str) -> list[datetime]:
    """Every bucket start covering [start, end) — the zero-filled x axis."""
    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    cur = floor_bucket(start, bucket)
    out: list[datetime] = []
    while cur < end and len(out) < 2000:
        out.append(cur)
        cur += step
    return out


def _edges(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Series bounds for an open-ended ('all') range."""
    return (start or (_utc_now() - timedelta(days=_ALL_TIME_DAYS)),
            end or (_utc_now() + timedelta(hours=1)))


def _ratio(a: float, b: float) -> float | None:
    return (a / b) if b else None


def _totals_from(r: Any) -> dict[str, Any]:
    d = _row(r)
    for k in ("calls", "tin", "tout", "cache_read", "cache_write", "ok_n"):
        d[k] = int(d[k])
    d["spend"] = float(d["spend"])
    d["avg_lat"] = float(d["avg_lat"]) if d["avg_lat"] is not None else None
    d["err_n"] = d["calls"] - d["ok_n"]
    d["success"] = _ratio(d["ok_n"] * 100.0, d["calls"])
    d["cache_hit"] = _ratio(d["cache_read"] * 100.0, d["tin"])
    return d


_TOTALS_SQL = (
    "SELECT COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS spend, "
    "COALESCE(SUM(tokens_in), 0) AS tin, COALESCE(SUM(tokens_out), 0) AS tout, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read, "
    "COALESCE(SUM(cache_write_tokens), 0) AS cache_write, "
    "COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_n, "
    "AVG(latency_ms) AS avg_lat FROM usage_log "
)


# ─── overview ───────────────────────────────────────────────────────────────


async def range_totals(start: datetime | None, end: datetime | None,
                       project_id: int | None = None) -> dict[str, Any]:
    """Totals over [start, end), optionally for one project."""
    p: dict[str, Any] = {}
    conds, p = _win(start, end, params=p)
    if project_id is not None:
        conds.insert(0, "project_id = :pid")
        p["pid"] = project_id
    async with get_session() as s:
        r = (await s.execute(text(_TOTALS_SQL + _where(conds)), p)).one()
    return _totals_from(r)


async def latency_percentile(start: datetime | None, end: datetime | None,
                             q: float = 0.95) -> int | None:
    """Nearest-rank percentile of successful-call latency (count + OFFSET: one
    code path for both databases)."""
    conds, p = _win(start, end)
    conds += ["status = 'ok'", "latency_ms IS NOT NULL"]
    async with get_session() as s:
        n = int((await s.execute(text(
            f"SELECT COUNT(*) FROM usage_log {_where(conds)}"), p)).scalar() or 0)
        if not n:
            return None
        p["off"] = max(0, math.ceil(q * n) - 1)
        v = (await s.execute(text(
            f"SELECT latency_ms FROM usage_log {_where(conds)} "
            "ORDER BY latency_ms LIMIT 1 OFFSET :off"), p)).scalar()
    return int(v) if v is not None else None


async def time_series(start: datetime, end: datetime, bucket: str) -> list[dict[str, Any]]:
    """Zero-filled per-bucket calls / ok / err / spend / cache / latency."""
    dialect = await _dialect()
    conds, p = _win(start, end)
    expr = _bucket_sql(bucket, dialect)
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT {expr} AS b, COUNT(*) AS calls, "
            "COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_n, "
            "COALESCE(SUM(cost_usd), 0) AS spend, "
            "COALESCE(SUM(tokens_in), 0) AS tin, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read, "
            "AVG(latency_ms) AS avg_lat "
            f"FROM usage_log {_where(conds)} GROUP BY b"), p)).all()
    by_b = {_dt(r.b): r for r in rows}
    out: list[dict[str, Any]] = []
    for b in bucket_starts(start, end, bucket):
        r = by_b.get(b)
        calls = int(r.calls) if r else 0
        ok_n = int(r.ok_n) if r else 0
        tin = int(r.tin) if r else 0
        cr = int(r.cache_read) if r else 0
        out.append({
            "ts": b, "calls": calls, "ok": ok_n, "err": calls - ok_n,
            "spend": float(r.spend) if r else 0.0,
            "success": _ratio(ok_n * 100.0, calls),
            "cache_hit": _ratio(cr * 100.0, tin),
            "avg_lat": float(r.avg_lat) if r and r.avg_lat is not None else None,
        })
    return out


def _top_names(per_model: dict[str, list[float]], n: int) -> list[str]:
    totals = {m: sum(v) for m, v in per_model.items()}
    ranked = sorted(per_model, key=lambda m: (-totals[m], m))
    return [m for m in ranked if totals[m] > 0][:n]


async def usage_by_model_series(start: datetime, end: datetime, bucket: str,
                                top: int = 6) -> dict[str, Any]:
    """Stacked-chart data: per bucket, the top-N models + 'other', for both the
    spend and the calls view (the free lanes cost $0, so spend alone is blank)."""
    dialect = await _dialect()
    conds, p = _win(start, end)
    expr = _bucket_sql(bucket, dialect)
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT {expr} AS b, COALESCE(model, provider) AS m, "
            "COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS spend "
            f"FROM usage_log {_where(conds)} GROUP BY b, m"), p)).all()
    buckets = bucket_starts(start, end, bucket)
    index = {b: i for i, b in enumerate(buckets)}
    metrics: dict[str, dict[str, list[float]]] = {"spend": {}, "calls": {}}
    for r in rows:
        i = index.get(_dt(r.b))
        if i is None:
            continue
        for metric, val in (("spend", float(r.spend)), ("calls", float(r.calls))):
            metrics[metric].setdefault(r.m, [0.0] * len(buckets))[i] += val
    out: dict[str, Any] = {"buckets": [b.isoformat() + "Z" for b in buckets]}
    for metric, per_model in metrics.items():
        names = _top_names(per_model, top)
        series = [per_model[m] for m in names]
        rest = [m for m in per_model if m not in names]
        if rest and any(any(per_model[m]) for m in rest):
            names.append("other")
            series.append([sum(per_model[m][i] for m in rest) for i in range(len(buckets))])
        out[metric] = {"names": names,
                       "series": [[round(x, 6) for x in s] for s in series]}
    return out


async def provider_activity(since: datetime) -> dict[str, dict[str, Any]]:
    """Calls / errors / spend per provider since `since` (the last hour feeds
    the provider health grid)."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT provider, COUNT(*) AS calls, "
            "COALESCE(SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END), 0) AS errs, "
            "COALESCE(SUM(cost_usd), 0) AS spend "
            "FROM usage_log WHERE created_at >= :since GROUP BY provider"),
            {"since": since})).all()
    return {r.provider: {"calls": int(r.calls), "errs": int(r.errs),
                         "spend": float(r.spend)} for r in rows}


# ─── projects ───────────────────────────────────────────────────────────────


async def project_range_stats(start: datetime | None, end: datetime | None,
                              bucket: str) -> dict[int, dict[str, Any]]:
    """Per project: range calls / ok / spend / tokens / cache plus a per-bucket
    calls series (the card sparkline). Projects with no traffic are absent."""
    dialect = await _dialect()
    cs, ce = _edges(start, end)
    conds, p = _win(start, end)
    conds.append("project_id IS NOT NULL")
    expr = _bucket_sql(bucket, dialect)
    async with get_session() as s:
        totals = (await s.execute(text(
            "SELECT project_id, COUNT(*) AS calls, COALESCE(SUM(cost_usd), 0) AS spend, "
            "COALESCE(SUM(tokens_in), 0) AS tin, "
            "COALESCE(SUM(cache_read_tokens), 0) AS cache_read, "
            "COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_n "
            f"FROM usage_log {_where(conds)} GROUP BY project_id"), p)).all()
        series = (await s.execute(text(
            f"SELECT project_id, {expr} AS b, COUNT(*) AS calls "
            f"FROM usage_log {_where(conds)} GROUP BY project_id, b"), p)).all()
    buckets = bucket_starts(cs, ce, bucket)
    index = {b: i for i, b in enumerate(buckets)}
    out: dict[int, dict[str, Any]] = {}
    for r in totals:
        calls, ok_n, tin, cr = int(r.calls), int(r.ok_n), int(r.tin), int(r.cache_read)
        out[int(r.project_id)] = {
            "calls": calls, "spend": float(r.spend), "tin": tin, "cache_read": cr,
            "ok_n": ok_n, "success": _ratio(ok_n * 100.0, calls),
            "cache_hit": _ratio(cr * 100.0, tin), "spark": [0] * len(buckets),
        }
    for r in series:
        i = index.get(_dt(r.b))
        if i is not None and int(r.project_id) in out:
            out[int(r.project_id)]["spark"][i] = int(r.calls)
    return out


async def project_breakdown(project_id: int, start: datetime | None, end: datetime | None,
                            bucket: str) -> dict[str, Any]:
    """Everything the project detail tabs show, in portable SQL."""
    cs, ce = _edges(start, end)
    conds, p = _win(start, end, "u.created_at", {"pid": project_id})
    conds.insert(0, "u.project_id = :pid")
    w = _where(conds)
    bexpr = _bucket_sql(bucket, await _dialect(), "u.created_at")

    async def grouped(key: str, limit: int = 0) -> list[dict[str, Any]]:
        async with get_session() as s:
            rows = (await s.execute(text(
                f"SELECT {key} AS k, COUNT(*) AS n, COALESCE(SUM(u.cost_usd), 0) AS spend, "
                "COALESCE(SUM(u.tokens_in), 0) AS tin, "
                "COALESCE(SUM(u.cache_read_tokens), 0) AS cache_r, "
                "COALESCE(SUM(CASE WHEN u.status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_n "
                f"FROM usage_log u {w} GROUP BY k "
                f"ORDER BY spend DESC, n DESC{f' LIMIT {int(limit)}' if limit else ''}"),
                p)).all()
        return [_row(r) for r in rows]

    async def spark_for(key: str) -> dict[str, list[int]]:
        async with get_session() as s:
            rows = (await s.execute(text(
                f"SELECT {key} AS k, {bexpr} AS b, COUNT(*) AS n "
                f"FROM usage_log u {w} GROUP BY k, b"), p)).all()
        buckets = bucket_starts(cs, ce, bucket)
        index = {b: i for i, b in enumerate(buckets)}
        out: dict[str, list[int]] = {}
        for r in rows:
            i = index.get(_dt(r.b))
            if i is not None:
                out.setdefault(r.k, [0] * len(buckets))[i] += int(r.n)
        return out

    edges = (0, *_LAT_EDGES_MS, 10**12)
    hist_cases = ", ".join(
        f"COALESCE(SUM(CASE WHEN u.latency_ms >= {lo} AND u.latency_ms < {hi} "
        f"THEN 1 ELSE 0 END), 0) AS h{i}"
        for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:], strict=True))
    )

    async def hist() -> list[int]:
        async with get_session() as s:
            r = (await s.execute(text(
                f"SELECT {hist_cases} FROM usage_log u {w} AND u.latency_ms IS NOT NULL"),
                p)).one()
        return [int(x or 0) for x in r]

    async def keys_used() -> list[dict[str, Any]]:
        async with get_session() as s:
            rows = (await s.execute(text(
                "SELECT u.api_key_id AS key_id, k.provider AS provider, k.label AS label, "
                "COUNT(*) AS n, COALESCE(SUM(u.cost_usd), 0) AS spend, "
                "COALESCE(SUM(CASE WHEN u.status <> 'ok' THEN 1 ELSE 0 END), 0) AS errs "
                f"FROM usage_log u LEFT JOIN api_keys k ON k.id = u.api_key_id {w} "
                "GROUP BY u.api_key_id, k.provider, k.label ORDER BY n DESC"), p)).all()
        return [_row(r) for r in rows]

    async def recent() -> list[dict[str, Any]]:
        async with get_session() as s:
            rows = (await s.execute(text(
                _REQUEST_SELECT + " WHERE u.project_id = :pid ORDER BY u.id DESC LIMIT 50"),
                {"pid": project_id})).all()
        return [_row(r, "created_at") for r in rows]

    (tot, by_provider, by_model, by_cap, by_wf, cap_sp, wf_sp, lat, keys, rec) = \
        await gather(
            range_totals(start, end, project_id),
            grouped("u.provider"),
            grouped("COALESCE(u.model, '(none)')", 12),
            grouped("COALESCE(u.capability, '(none)')"),
            grouped("COALESCE(u.workflow, '(none)')"),
            spark_for("COALESCE(u.capability, '(none)')"),
            spark_for("COALESCE(u.workflow, '(none)')"),
            hist(), keys_used(), recent())
    return {
        "totals": tot, "by_provider": by_provider, "by_model": by_model,
        "by_capability": by_cap, "by_workflow": by_wf,
        "cap_spark": cap_sp, "wf_spark": wf_sp,
        "lat_hist": list(zip(_LAT_LABELS, lat, strict=True)),
        "used_keys": keys, "recent": rec,
    }


# ─── requests ───────────────────────────────────────────────────────────────

_REQUEST_SELECT = (
    "SELECT u.id, u.created_at, u.project_id, p.name AS project, u.provider, u.model, "
    "u.model_served, u.capability, u.workflow, u.tokens_in, u.tokens_out, "
    "u.cache_read_tokens, u.cache_write_tokens, u.cost_usd, u.latency_ms, u.status, "
    "u.http_status, u.error_kind, u.api_key_id, u.request_id, k.label AS key_label "
    "FROM usage_log u LEFT JOIN projects p ON p.id = u.project_id "
    "LEFT JOIN api_keys k ON k.id = u.api_key_id"
)


def _opt_int(v: Any) -> int | None:
    try:
        n = int(str(v).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _opt_float(v: Any) -> float | None:
    try:
        x = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x >= 0 else None


def _opt_str(v: Any, maxlen: int = 120) -> str | None:
    s = (str(v).strip() if v is not None else "")[:maxlen]
    return s or None


@dataclass
class RequestFilter:
    start: datetime | None = None
    end: datetime | None = None
    project_id: int | None = None
    workflow: str | None = None
    capability: str | None = None
    provider: str | None = None
    model: str | None = None
    status: str | None = None          # ok | error
    min_cost: float | None = None
    min_latency: int | None = None
    request_id: int | None = None
    sort: str = "time"
    desc: bool = True
    page: int = 0

    @classmethod
    def from_params(cls, params: Mapping[str, str], start: datetime | None,
                    end: datetime | None) -> RequestFilter:
        status = params.get("status")
        sort = params.get("sort", "time")
        return cls(
            start=start, end=end,
            project_id=_opt_int(params.get("project")),
            workflow=_opt_str(params.get("workflow")),
            capability=_opt_str(params.get("capability")),
            provider=_opt_str(params.get("provider")),
            model=_opt_str(params.get("model")),
            status=status if status in ("ok", "error") else None,
            min_cost=_opt_float(params.get("min_cost")),
            min_latency=_opt_int(params.get("min_latency")),
            request_id=_opt_int(params.get("id")),
            sort=sort if sort in _SORTS else "time",
            desc=params.get("dir", "desc") != "asc",
            page=min(_opt_int(params.get("page")) or 0, 10_000),
        )

    def conditions(self) -> tuple[list[str], dict[str, Any]]:
        conds, p = _win(self.start, self.end, "u.created_at")
        if self.request_id is not None:
            conds.append("u.id = :rid")
            p["rid"] = self.request_id
        for col, val, key in (
            ("u.project_id", self.project_id, "proj"), ("u.workflow", self.workflow, "wf"),
            ("u.capability", self.capability, "cap"), ("u.provider", self.provider, "prov"),
            ("u.model", self.model, "mod"),
        ):
            if val is not None:
                conds.append(f"{col} = :{key}")
                p[key] = val
        if self.status == "ok":
            conds.append("u.status = 'ok'")
        elif self.status == "error":
            conds.append("u.status <> 'ok'")
        if self.min_cost is not None:
            conds.append("u.cost_usd >= :min_cost")
            p["min_cost"] = self.min_cost
        if self.min_latency is not None:
            conds.append("u.latency_ms >= :min_lat")
            p["min_lat"] = self.min_latency
        return conds, p

    @property
    def order_by(self) -> str:
        d = "DESC" if self.desc else "ASC"
        col = _SORTS[self.sort]
        return f"{col} {d}" if self.sort == "time" else f"{col} {d}, u.id DESC"

    def as_query(self) -> dict[str, str]:
        """Active filters as query params (sans range/page) — for links."""
        q: dict[str, str] = {}
        for key, val in (("project", self.project_id), ("workflow", self.workflow),
                         ("capability", self.capability), ("provider", self.provider),
                         ("model", self.model), ("status", self.status),
                         ("min_cost", self.min_cost), ("min_latency", self.min_latency),
                         ("id", self.request_id)):
            if val is not None:
                q[key] = str(val)
        if self.sort != "time" or not self.desc:
            q["sort"] = self.sort
            q["dir"] = "desc" if self.desc else "asc"
        return q


async def query_requests(f: RequestFilter, *, page_size: int = REQUEST_PAGE_SIZE,
                         limit: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """One page of usage_log rows (+ whether another page exists). `limit`
    overrides paging for the CSV export (a single bounded query)."""
    conds, p = f.conditions()
    if limit is not None:
        p.update(take=limit, offset=0)
    else:
        p.update(take=page_size + 1, offset=f.page * page_size)
    async with get_session() as s:
        rows = (await s.execute(text(
            f"{_REQUEST_SELECT} {_where(conds)} ORDER BY {f.order_by} "
            "LIMIT :take OFFSET :offset"), p)).all()
    out = [_row(r, "created_at") for r in rows]
    if limit is not None:
        return out, False
    return out[:page_size], len(out) > page_size


async def request_facets(since: datetime) -> dict[str, list[str]]:
    """Distinct workflow / model values (recent, most-used first) for the
    filter datalists. Capped, so it stays cheap on a large usage_log."""
    async def top(col: str) -> list[str]:
        async with get_session() as s:
            rows = (await s.execute(text(
                f"SELECT {col} AS v, COUNT(*) AS c FROM usage_log "
                f"WHERE created_at >= :since AND {col} IS NOT NULL "
                "GROUP BY v ORDER BY c DESC LIMIT 60"), {"since": since})).all()
        return [str(r.v) for r in rows]
    workflows, models = await gather(top("workflow"), top("model"))
    return {"workflow": workflows, "model": models}


async def get_request(request_id: int) -> dict[str, Any] | None:
    async with get_session() as s:
        r = (await s.execute(text(f"{_REQUEST_SELECT} WHERE u.id = :id"),
                             {"id": request_id})).first()
    return _row(r, "created_at") if r else None


# usage_log has no request / lease / job id linking the attempts of one request
# (lease_id is always NULL), so the fallback trail is INFERRED: the attempts of
# one walk run back to back, which puts attempt N+1's start (created_at minus
# its latency) right on attempt N's end (created_at).
_CHAIN_TOLERANCE_S = 5.0
_CHAIN_WINDOW = timedelta(minutes=20)
_CHAIN_MAX_ROWS = 600


def _start_of(r: Mapping[str, Any]) -> datetime:
    return r["created_at"] - timedelta(milliseconds=int(r["latency_ms"] or 0))


def link_attempts(anchor: dict[str, Any], cands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order the attempts of the request `anchor` belongs to. Pure (testable):
    walks backwards through failed predecessors and forwards through successors
    whose start matches the previous attempt's end."""
    same = [c for c in cands if c["id"] != anchor["id"]]
    chain = [anchor]
    used = {anchor["id"]}

    def closest(gap_of: Any, ok_only_failed: bool) -> dict[str, Any] | None:
        best: tuple[float, dict[str, Any]] | None = None
        for c in same:
            if c["id"] in used or (ok_only_failed and c["status"] == "ok"):
                continue
            gap = abs(gap_of(c))
            if gap <= _CHAIN_TOLERANCE_S and (best is None or gap < best[0]):
                best = (gap, c)
        return best[1] if best else None

    cur = anchor
    while True:   # backwards: a predecessor is a FAILED attempt ending as `cur` began
        nxt = closest(lambda c, cur=cur: (_start_of(cur) - c["created_at"]).total_seconds(), True)
        if nxt is None:
            break
        chain.insert(0, nxt)
        used.add(nxt["id"])
        cur = nxt
    cur = anchor
    while cur["status"] != "ok":   # forwards: only a failure is followed by another try
        nxt = closest(lambda c, cur=cur: (_start_of(c) - cur["created_at"]).total_seconds(), False)
        if nxt is None:
            break
        chain.append(nxt)
        used.add(nxt["id"])
        cur = nxt
    return chain


async def request_attempts(row: dict[str, Any]) -> list[dict[str, Any]]:
    """The attempt trail for one usage_log row. Rows carrying a request_id
    (migration 015) are grouped by it exactly; older rows (NULL) keep the timing
    inference below (see _CHAIN_*)."""
    if row.get("request_id"):
        async with get_session() as s:
            rows = (await s.execute(text(
                f"{_REQUEST_SELECT} WHERE u.request_id = :rid ORDER BY u.id"),
                {"rid": row["request_id"]})).all()
        return [_row(r, "created_at") for r in rows]
    p: dict[str, Any] = {
        "lo": row["created_at"] - _CHAIN_WINDOW, "hi": row["created_at"] + _CHAIN_WINDOW,
        "take": _CHAIN_MAX_ROWS,
    }
    conds = ["u.created_at >= :lo", "u.created_at <= :hi"]
    for col, key, val in (("u.project_id", "proj", row["project_id"]),
                          ("u.capability", "cap", row["capability"]),
                          ("u.workflow", "wf", row["workflow"])):
        if val is None:
            conds.append(f"{col} IS NULL")
        else:
            conds.append(f"{col} = :{key}")
            p[key] = val
    async with get_session() as s:
        rows = (await s.execute(text(
            f"{_REQUEST_SELECT} {_where(conds)} ORDER BY u.id LIMIT :take"), p)).all()
    return link_attempts(row, [_row(r, "created_at") for r in rows])


# ─── keys / models ──────────────────────────────────────────────────────────


async def key_activity(since: datetime) -> dict[int, dict[str, Any]]:
    """Per api_key_id since `since`: calls, errors, last success / last error."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT api_key_id, COUNT(*) AS calls, "
            "COALESCE(SUM(CASE WHEN status <> 'ok' THEN 1 ELSE 0 END), 0) AS errs, "
            "MAX(CASE WHEN status = 'ok' THEN created_at END) AS last_ok, "
            "MAX(CASE WHEN status <> 'ok' THEN created_at END) AS last_err "
            "FROM usage_log WHERE api_key_id IS NOT NULL AND created_at >= :since "
            "GROUP BY api_key_id"), {"since": since})).all()
    return {int(r.api_key_id): {"calls": int(r.calls), "errs": int(r.errs),
                                "last_ok": _dt(r.last_ok), "last_err": _dt(r.last_err)}
            for r in rows}


async def observed_model_stats(since: datetime) -> dict[tuple[str, str], dict[str, Any]]:
    """(provider, model) → calls, ok, success %, p50 latency of successful calls.
    The median comes from a window function (nearest rank), portable."""
    async with get_session() as s:
        counts = (await s.execute(text(
            "SELECT provider, model, COUNT(*) AS calls, "
            "COALESCE(SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_n "
            "FROM usage_log WHERE model IS NOT NULL AND created_at >= :since "
            "GROUP BY provider, model"), {"since": since})).all()
        p50 = (await s.execute(text(
            "SELECT provider, model, latency_ms FROM ("
            " SELECT provider, model, latency_ms, "
            "  ROW_NUMBER() OVER (PARTITION BY provider, model ORDER BY latency_ms) AS rn, "
            "  COUNT(*) OVER (PARTITION BY provider, model) AS cnt "
            " FROM usage_log WHERE status = 'ok' AND latency_ms IS NOT NULL "
            "  AND model IS NOT NULL AND created_at >= :since) t "
            "WHERE rn = (cnt + 1) / 2"), {"since": since})).all()
    med = {(r.provider, r.model): int(r.latency_ms) for r in p50}
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in counts:
        calls, ok_n = int(r.calls), int(r.ok_n)
        out[(r.provider, r.model)] = {
            "calls": calls, "ok": ok_n, "success": _ratio(ok_n * 100.0, calls),
            "p50": med.get((r.provider, r.model)),
        }
    return out


# ─── jobs / audit ───────────────────────────────────────────────────────────


async def job_overview() -> dict[str, Any]:
    now = _utc_now()
    async with get_session() as s:
        counts = (await s.execute(text(
            "SELECT status, COUNT(*) AS n FROM deep_jobs GROUP BY status"))).all()
        oldest = (await s.execute(text(
            "SELECT MIN(created_at) FROM deep_jobs WHERE status = 'pending'"))).scalar()
        longest = (await s.execute(text(
            "SELECT MIN(started_at) FROM deep_jobs WHERE status = 'running'"))).scalar()
        recent = (await s.execute(text(
            "SELECT j.id, j.project_id, p.name AS project, j.capability, j.status, "
            "j.retry_count, j.created_at, j.started_at, j.completed_at, j.run_after, "
            "j.error_message "
            "FROM deep_jobs j LEFT JOIN projects p ON p.id = j.project_id "
            "ORDER BY j.id DESC LIMIT 50"))).all()
    oldest_pending, oldest_running = _dt(oldest), _dt(longest)
    return {
        "by_status": {r.status: int(r.n) for r in counts},
        "oldest_pending": oldest_pending, "oldest_running": oldest_running,
        "stuck_pending": bool(oldest_pending
                              and now - oldest_pending > timedelta(minutes=STUCK_PENDING_MIN)),
        "stuck_running": bool(oldest_running
                              and now - oldest_running > timedelta(minutes=STUCK_RUNNING_MIN)),
        "recent": [_row(r, "created_at", "started_at", "completed_at", "run_after")
                   for r in recent],
    }


async def audit_page(*, before_id: int | None, actor: str | None,
                     action: str | None) -> tuple[list[dict[str, Any]], bool]:
    q = select(AuditLogRow).order_by(AuditLogRow.id.desc()).limit(AUDIT_PAGE_SIZE + 1)
    if before_id is not None:
        q = q.where(AuditLogRow.id < before_id)
    if actor:
        q = q.where(AuditLogRow.actor == actor)
    if action:
        like = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        q = q.where(AuditLogRow.action.like(like, escape="\\"))
    async with get_session() as s:
        rows = list((await s.execute(q)).scalars().all())
    out = [{"id": r.id, "actor": r.actor, "action": r.action, "target": r.target,
            "metadata": r.metadata_ or {}, "ip": r.ip, "created_at": r.created_at}
           for r in rows[:AUDIT_PAGE_SIZE]]
    return out, len(rows) > AUDIT_PAGE_SIZE
