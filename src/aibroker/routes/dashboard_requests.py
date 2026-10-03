"""The unified Requests list: ONE row per CLIENT request.

`usage_log` holds one row per provider ATTEMPT; the attempts of one client
request share `request_id` (a uuid for direct calls, `job-<id>` for queued jobs,
migration 015). Rows predating it have NULL and each count as their own request
(`u-<usage id>`). Queued jobs (`deep_jobs`) are LEFT JOINed on `job-<id>`, and a
job that has no attempt yet (pending / running) still appears, straight from
`deep_jobs`.

Aggregation happens in the database inside the range window (a bounded scan of
`ix_usage_created_at`), and only the requested page is joined to its served
attempt / project / key — so the cost follows the window, not the table.
Portable SQL: the same text runs on Postgres and on the SQLite test database.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.routes.dashboard_queries import (
    _opt_int,
    _opt_str,
    _row,
    _utc_now,
    _where,
    _win,
    get_job,
    get_request,
    request_attempts,
)

REQUEST_PAGE_SIZE = 50
CSV_EXPORT_LIMIT = 20_000

# Newest-first pages are cut from a narrow slice of the window first (1 d, 7 d,
# 30 d, then the whole range): the first page is nearly always inside the last
# day, and aggregating a day is ~100x cheaper than aggregating the retention
# window. A slice is only trusted when it alone fills the page, and it reaches
# `_MARGIN` further back than it keeps, so a request that started just before
# the cut is still aggregated whole.
_SLICES = (timedelta(days=1), timedelta(days=7), timedelta(days=30))
_MARGIN = timedelta(days=1)

STATUSES = ("ok", "failed", "pending", "running")
KINDS = ("job", "direct")

_GK = "COALESCE(u.request_id, 'u-' || CAST(u.id AS VARCHAR(20)))"
_JOB_GK = "'job-' || CAST(j.id AS VARCHAR(20))"
_JOB_PREFIX = "job-"
_SEARCH_RE = re.compile(r"(?:(job|u)-)?(\d+)")

# Whitelisted ORDER BY (alias-prefixed at use) — user input only selects a key.
_SORTS: dict[str, str] = {
    "time": "{a}sort_time DESC, {a}gk DESC",
    "cost": "{a}cost_usd DESC, {a}sort_time DESC",
    "latency": "{a}latency_ms DESC, {a}sort_time DESC",
    "tokens": "({a}tokens_in + {a}tokens_out) DESC, {a}sort_time DESC",
}
_ASC = {"time": "{a}sort_time ASC, {a}gk ASC", "cost": "{a}cost_usd ASC, {a}sort_time DESC",
        "latency": "{a}latency_ms ASC, {a}sort_time DESC",
        "tokens": "({a}tokens_in + {a}tokens_out) ASC, {a}sort_time DESC"}

# The one place a request's state is derived: a job's own state wins while it is
# live / when it ended; otherwise a request is ok iff some attempt succeeded.
_STATE_SQL = (
    "CASE WHEN r.job_status IN ('pending', 'running') THEN r.job_status "
    "WHEN r.job_status = 'done' THEN 'ok' WHEN r.job_status = 'error' THEN 'failed' "
    "WHEN r.ok_id IS NOT NULL THEN 'ok' ELSE 'failed' END"
)

_GROUPED = (
    f"SELECT {_GK} AS gk, MIN(u.id) AS first_id, MAX(u.id) AS last_id, "
    "MAX(CASE WHEN u.status = 'ok' THEN u.id END) AS ok_id, COUNT(*) AS tries, "
    "MIN(u.created_at) AS first_at, MAX(u.project_id) AS project_id, "
    "MAX(u.capability) AS capability, MAX(u.workflow) AS workflow, "
    "SUM(u.cost_usd) AS cost_usd, SUM(u.tokens_in) AS tokens_in, "
    "SUM(u.tokens_out) AS tokens_out, SUM(u.cache_read_tokens) AS cache_read_tokens, "
    "SUM(u.cache_write_tokens) AS cache_write_tokens, "
    "COALESCE(SUM(u.latency_ms), 0) AS latency_ms{extra} "
    "FROM usage_log u {where} GROUP BY {gk}"
)
_JOB_COLS = ("j.id AS job_id, j.status AS job_status, j.created_at AS job_created, "
             "j.started_at AS job_started, j.completed_at AS job_completed, "
             "j.retry_count AS job_retries, j.error_message AS job_error")


def search_target(term: str | None) -> tuple[str, int | None] | None:
    """Parse the request-id search box into (kind, number): `job-12` / `12` can
    be a job or a legacy single row, `u-77` a legacy row; anything else is an
    opaque request id matched exactly. None when empty."""
    if not term:
        return None
    m = _SEARCH_RE.fullmatch(term)
    if not m:
        return ("rid", None)
    return (m.group(1) or "any", int(m.group(2)))


@dataclass
class RequestFilter:
    start: datetime | None = None
    end: datetime | None = None
    project_id: int | None = None
    workflow: str | None = None
    capability: str | None = None
    provider: str | None = None        # any attempt went to this provider
    model: str | None = None           # any attempt was routed to this model
    status: str | None = None          # ok | failed | pending | running
    kind: str | None = None            # job | direct
    search: str | None = None          # request id (exact)
    sort: str = "time"
    desc: bool = True
    page: int = 0

    @classmethod
    def from_params(cls, params: Mapping[str, str], start: datetime | None,
                    end: datetime | None) -> RequestFilter:
        status = {"error": "failed"}.get(params.get("status", ""), params.get("status"))
        sort = params.get("sort", "time")
        kind = params.get("type")
        return cls(
            start=start, end=end,
            project_id=_opt_int(params.get("project")),
            workflow=_opt_str(params.get("workflow")),
            capability=_opt_str(params.get("capability")),
            provider=_opt_str(params.get("provider")),
            model=_opt_str(params.get("model")),
            status=status if status in STATUSES else None,
            kind=kind if kind in KINDS else None,
            search=_opt_str(params.get("q"), 64),
            sort=sort if sort in _SORTS else "time",
            desc=params.get("dir", "desc") != "asc",
            page=min(_opt_int(params.get("page")) or 0, 10_000),
        )

    def as_query(self) -> dict[str, str]:
        """Active filters as query params (sans range/page) — for links."""
        out: dict[str, str] = {}
        for key, val in (("project", self.project_id), ("workflow", self.workflow),
                         ("capability", self.capability), ("provider", self.provider),
                         ("model", self.model), ("status", self.status), ("type", self.kind), ("q", self.search)):
            if val is not None:
                out[key] = str(val)
        if self.sort != "time" or not self.desc:
            out["sort"] = self.sort
            out["dir"] = "desc" if self.desc else "asc"
        return out

    def order_by(self, alias: str) -> str:
        return (_SORTS if self.desc else _ASC)[self.sort].format(a=alias)

    def _usage_conditions(self, since: datetime | None) -> tuple[list[str], dict[str, Any]]:
        """Attempt-level conditions — every attempt of a request shares these,
        so filtering rows here never splits a request."""
        lo = self.start if since is None else since - _MARGIN
        conds, p = _win(lo if self.start is None or lo is None else max(lo, self.start),
                        self.end, "u.created_at")
        for col, val, key in (("u.project_id", self.project_id, "proj"),
                              ("u.workflow", self.workflow, "wf"),
                              ("u.capability", self.capability, "cap")):
            if val is not None:
                conds.append(f"{col} = :{key}")
                p[key] = val
        if self.kind == "job":
            conds.append(f"u.request_id LIKE '{_JOB_PREFIX}%'")
        elif self.kind == "direct":
            conds.append(f"(u.request_id IS NULL OR u.request_id NOT LIKE '{_JOB_PREFIX}%')")
        target = search_target(self.search)
        if target:
            kind, num = target
            if kind == "rid":
                conds.append("u.request_id = :rid")
                p["rid"] = self.search
            else:
                legacy = "(u.request_id IS NULL AND u.id = :num)" if kind != "job" else "1 = 0"
                job = "u.request_id = :jobrid" if kind != "u" else "1 = 0"
                conds.append(f"({job} OR {legacy})")
                p.update(num=num, jobrid=f"{_JOB_PREFIX}{num}")
        return conds, p

    def _job_conditions(self, since: datetime | None) -> tuple[list[str], dict[str, Any]] | None:
        """Conditions for jobs WITHOUT attempts (they only exist in deep_jobs),
        or None when this filter can never match one."""
        if self.provider or self.model or self.workflow or self.kind == "direct":
            return None
        conds, p = _win(self.start if since is None else max(since, self.start or since),
                        self.end, "j.created_at")
        for col, val, key in (("j.project_id", self.project_id, "proj"),
                              ("j.capability", self.capability, "cap")):
            if val is not None:
                conds.append(f"{col} = :{key}")
                p[key] = val
        target = search_target(self.search)
        if target:
            kind, num = target
            if kind not in ("job", "any"):
                return None
            conds.append("j.id = :num")
            p["num"] = num
        conds.append("NOT EXISTS (SELECT 1 FROM usage_log x WHERE x.request_id = "
                     f"{_JOB_GK})")
        return conds, p

    def _hit_sql(self) -> str:
        """Per-attempt test behind the provider / model filters (a request
        matches when ANY of its attempts does)."""
        return " AND ".join(c for c, v in (("u.provider = :prov", self.provider),
                                           ("u.model = :mod", self.model)) if v)

    def _outer_conditions(self, since: datetime | None) -> tuple[list[str], dict[str, Any]]:
        conds: list[str] = []
        p: dict[str, Any] = {}
        if since is not None:
            conds.append("r2.sort_time >= :since")
            p["since"] = since
        if self.status:
            conds.append("r2.state = :state")
            p["state"] = self.status
        if self._hit_sql():
            conds.append("r2.prov_hit = 1")
        return conds, p

    def sql(self, since: datetime | None = None) -> tuple[str, dict[str, Any]]:
        """The CTE chain up to (and including) the filtered, ordered page
        window `pg`; `take` / `offset` are bound by the caller. `since` keeps
        only requests that started at or after it (see _SLICES)."""
        uc, p = self._usage_conditions(since)
        extra = ""
        if hit := self._hit_sql():
            extra = f", MAX(CASE WHEN {hit} THEN 1 ELSE 0 END) AS prov_hit"
            p.update({k: v for k, v in (("prov", self.provider), ("mod", self.model)) if v})
        grouped = _GROUPED.format(extra=extra, where=_where(uc), gk=_GK)
        prov_cols = ", g.prov_hit" if hit else ""
        joined = (
            "SELECT g.gk, g.first_id, g.last_id, g.ok_id, g.tries, g.first_at, "
            "g.project_id, g.capability, g.workflow, g.cost_usd, g.tokens_in, "
            "g.tokens_out, g.cache_read_tokens, g.cache_write_tokens, g.latency_ms"
            f"{prov_cols}, {_JOB_COLS} FROM g "
            f"LEFT JOIN deep_jobs j ON g.gk = {_JOB_GK}"
        )
        jc = self._job_conditions(since)
        if jc is not None:
            conds, jp = jc
            p.update(jp)
            joined += (
                f" UNION ALL SELECT {_JOB_GK}, NULL, NULL, NULL, 0, NULL, j.project_id, "
                "j.capability, NULL, 0.0, 0, 0, 0, 0, 0, "
                f"{_JOB_COLS} FROM deep_jobs j {_where(conds)}"
            )
        oc, op = self._outer_conditions(since)
        p.update(op)
        sql = (
            f"WITH g AS ({grouped}), r AS ({joined}), "
            f"r2 AS (SELECT r.*, {_STATE_SQL} AS state, "
            "COALESCE(r.job_created, r.first_at) AS sort_time, "
            "COALESCE(r.ok_id, r.last_id) AS served_id FROM r), "
            f"pg AS (SELECT * FROM r2 {_where(oc)} ORDER BY {self.order_by('r2.')} "
            "LIMIT :take OFFSET :offset) "
        )
        return sql, p


_FINAL = (
    "SELECT pg.*, p.name AS project, s.provider, s.model, s.model_served, "
    "s.status AS served_status, s.http_status, s.error_kind, k.label AS key_label "
    "FROM pg LEFT JOIN projects p ON p.id = pg.project_id "
    "LEFT JOIN usage_log s ON s.id = pg.served_id "
    "LEFT JOIN api_keys k ON k.id = s.api_key_id"
)


def _shape(r: Any, now: datetime) -> dict[str, Any]:
    """Row → template/CSV dict: datetimes parsed, `ref` (drawer key), `kind`,
    queue wait (jobs only) and the request's start time."""
    d = _row(r, "first_at", "job_created", "job_started", "job_completed")
    d["created_at"] = d["job_created"] or d["first_at"]
    d["is_job"] = d["job_id"] is not None
    d["ref"] = f"{_JOB_PREFIX}{d['job_id']}" if d["is_job"] else str(d["first_id"])
    d["request_id"] = d["gk"]
    d["wait_ms"] = None
    if d["is_job"]:
        end = d["job_started"] if d["state"] != "pending" and d["job_started"] else now
        d["wait_ms"] = max(0, int((end - d["job_created"]).total_seconds() * 1000))
    d["cost_usd"] = float(d["cost_usd"] or 0.0)
    return d


async def _fetch(f: RequestFilter, since: datetime | None, take: int, offset: int
                 ) -> list[dict[str, Any]]:
    head, p = f.sql(since)
    p.update(take=take, offset=offset)
    async with get_session() as s:
        rows = (await s.execute(text(
            f"{head}{_FINAL} ORDER BY {f.order_by('pg.')}"), p)).all()
    now = _utc_now()
    return [_shape(r, now) for r in rows]


async def query_requests(f: RequestFilter, *, page_size: int = REQUEST_PAGE_SIZE,
                         limit: int | None = None) -> tuple[list[dict[str, Any]], bool]:
    """One page of client requests (+ whether another page exists). `limit`
    overrides paging for the CSV export (a single bounded query)."""
    if limit is not None:
        return await _fetch(f, None, limit, 0), False
    offset = f.page * page_size
    if f.sort == "time" and f.desc and not f.search:   # a search is already index-selective
        now = _utc_now()
        for span in _SLICES:
            since = now - span
            if f.start is not None and since <= f.start:
                break
            rows = await _fetch(f, since, page_size + 1, offset)
            if len(rows) > page_size:          # the slice alone fills the page
                return rows[:page_size], True
    rows = await _fetch(f, None, page_size + 1, offset)
    return rows[:page_size], len(rows) > page_size


async def request_detail(ref: str) -> dict[str, Any] | None:
    """Everything the drawer shows for one list row (`ref` is the row's drawer
    key: a usage_log id, or `job-<id>`): the summary row (same shape and same
    SQL as the list, so totals and state never disagree), the job, and every
    attempt. None when nothing of it is left (history purged)."""
    job_m = re.fullmatch(rf"{_JOB_PREFIX}(\d+)", ref)
    row: dict[str, Any] | None = None
    if job_m:
        rid: str | None = ref
    elif ref.isdigit():
        row = await get_request(int(ref))
        if row is None:
            return None
        rid = row["request_id"]
    else:
        return None
    attempts = await request_attempts(rid) if rid else [row]
    if rid and (m := re.fullmatch(rf"{_JOB_PREFIX}(\d+)", rid)):
        job = await get_job(int(m.group(1)))
    else:
        job = None
    if not attempts and job is None:
        return None
    found, _ = await query_requests(RequestFilter(search=rid or f"u-{ref}"), page_size=1)
    if not found:
        return None
    return {"summary": found[0], "job": job, "attempts": attempts}
