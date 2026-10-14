"""Subscription agent runs on the project page.

`agent:codex` / `agent:claude` jobs (deep_jobs + agent_runs) never write
`usage_log`, so the usage-only project breakdown cannot see them. This module
aggregates them separately and folds them into the same shape
(`project_breakdown_with_agents`).

No double counting: a plain job's provider attempts live in `usage_log` and are
counted there only; agent jobs are counted here only (`capability LIKE 'agent:%'`
on deep_jobs, which have no usage_log rows). Cost is always 0 (subscription).
One grouped query over the (project_id, created_at) window; only the
result_meta keys it needs are read, never the request body.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.routes import dashboard_queries as q
from aibroker.routes import dashboard_requests as rq

AGENT_RECENT_LIMIT = 50
# Shown when the run did not report its own provider (result_meta.provider).
_PROVIDER_BY_CAP = {"agent:codex": "codex-sub", "agent:claude": "claude-sub"}
_NO_MODEL = "(none)"
# Cache-hit denominator. The Claude CLI reports input_tokens EXCLUDING cache reads
# (agent/parsers.py: usage.input_tokens vs cache_read_input_tokens), so its
# denominator is input + cache reads. Codex `cached_input_tokens` / app-server
# `cachedInputTokens` are a SUBSET of the input count, so its denominator is input.
_CACHE_EXCLUDED_FROM_INPUT = frozenset({"agent:claude"})


def agent_provider(capability: str, reported: str | None) -> str:
    """Provider label of an agent job: what the run reported, else derived from
    the capability (agent:codex -> codex-sub, agent:claude -> claude-sub)."""
    return reported or _PROVIDER_BY_CAP.get(capability, capability)


def _bucket_row() -> dict[str, Any]:
    return {"n": 0, "spend": 0.0, "tin": 0, "tout": 0, "cache_r": 0, "ok_n": 0, "cdenom": 0}


def cache_denominator(capability: str, tin: int, cache_r: int) -> int:
    """Input tokens a cache hit is measured against (see the rule above)."""
    return tin + (cache_r if capability in _CACHE_EXCLUDED_FROM_INPUT else 0)


def _add(into: dict[str, dict[str, Any]], key: str, r: Any) -> None:
    b = into.setdefault(key, _bucket_row())
    b["n"] += int(r.n)
    b["tin"] += int(r.tin)
    b["tout"] += int(r.tout)
    b["cache_r"] += int(r.cache_r)
    b["cdenom"] += cache_denominator(r.a_cap, int(r.tin), int(r.cache_r))
    if r.a_status == "done":
        b["ok_n"] += int(r.n)


def _rows(into: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"k": k, **v} for k, v in into.items()]


async def agent_breakdown(project_id: int, start: datetime | None, end: datetime | None,
                          bucket: str) -> dict[str, Any]:
    """Agent-job aggregates of one project in [start, end): totals, per
    provider / model / capability and a per-bucket calls series."""
    conds, p = q._win(start, end, "j.created_at", {"pid": project_id})
    # Self-enforcing no-double-count rule: a job with usage_log rows is counted there.
    conds = ["j.project_id = :pid", *conds, "j.capability LIKE 'agent:%'",
             "NOT EXISTS (SELECT 1 FROM usage_log x WHERE x.request_id = "
             "'job-' || CAST(j.id AS VARCHAR(20)))"]
    w = q._where(conds)
    bexpr = q._bucket_sql(bucket, await q._dialect(), "j.created_at")
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT j.capability AS a_cap, j.result_meta ->> 'provider' AS a_prov, "
            "ar.model AS a_model, j.result_meta ->> 'model_served' AS a_served, "
            "j.status AS a_status, COUNT(*) AS n, "
            "COALESCE(SUM(ar.usage_input_tokens), 0) AS tin, "
            "COALESCE(SUM(ar.usage_output_tokens), 0) AS tout, "
            "COALESCE(SUM(ar.usage_cache_read_tokens), 0) AS cache_r "
            f"FROM deep_jobs j LEFT JOIN agent_runs ar ON ar.run_id = j.id {w} "
            "GROUP BY j.capability, j.result_meta ->> 'provider', ar.model, "
            "j.result_meta ->> 'model_served', j.status"), p)).all()
        series = (await s.execute(text(
            f"SELECT j.capability AS a_cap, {bexpr} AS b, COUNT(*) AS n "
            f"FROM deep_jobs j {w} GROUP BY j.capability, b"), p)).all()
    prov: dict[str, dict[str, Any]] = {}
    model: dict[str, dict[str, Any]] = {}
    cap: dict[str, dict[str, Any]] = {}
    served: dict[str, set[str]] = {}
    status: dict[str, int] = {}
    tot = {"tin": 0, "tout": 0, "cache_r": 0, "cdenom": 0}
    for r in rows:
        _add(prov, agent_provider(r.a_cap, r.a_prov), r)
        pin = r.a_model or _NO_MODEL
        _add(model, pin, r)
        _add(cap, r.a_cap, r)
        if r.a_served:
            served.setdefault(pin, set()).add(r.a_served)
        status[r.a_status] = status.get(r.a_status, 0) + int(r.n)
        tot["tin"] += int(r.tin)
        tot["tout"] += int(r.tout)
        tot["cache_r"] += int(r.cache_r)
        tot["cdenom"] += cache_denominator(r.a_cap, int(r.tin), int(r.cache_r))
    cs, ce = q._edges(start, end)
    buckets = q.bucket_starts(cs, ce, bucket)
    index = {b: i for i, b in enumerate(buckets)}
    by_bucket = [0] * len(buckets)
    cap_spark: dict[str, list[int]] = {}
    for r in series:
        i = index.get(q._dt(r.b))
        if i is not None:
            cap_spark.setdefault(r.a_cap, [0] * len(buckets))[i] += int(r.n)
            by_bucket[i] += int(r.n)
    by_model = _rows(model)
    for m in by_model:
        m["served"] = sorted(served.get(m["k"], ()))
    return {
        "n": sum(status.values()), "done": status.get("done", 0),
        "error": status.get("error", 0), "cancelled": status.get("cancelled", 0),
        "active": status.get("pending", 0) + status.get("running", 0), **tot,
        "by_provider": _rows(prov), "by_model": by_model, "by_capability": _rows(cap),
        "cap_spark": cap_spark, "by_bucket": by_bucket,
    }


async def _usage_by_bucket(project_id: int, start: datetime | None, end: datetime | None,
                           bucket: str) -> list[int]:
    conds, p = q._win(start, end, "created_at", {"pid": project_id})
    conds.insert(0, "project_id = :pid")
    bexpr = q._bucket_sql(bucket, await q._dialect())
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT {bexpr} AS b, COUNT(*) AS n FROM usage_log {q._where(conds)} GROUP BY b"),
            p)).all()
    cs, ce = q._edges(start, end)
    buckets = q.bucket_starts(cs, ce, bucket)
    index = {b: i for i, b in enumerate(buckets)}
    out = [0] * len(buckets)
    for r in rows:
        i = index.get(q._dt(r.b))
        if i is not None:
            out[i] += int(r.n)
    return out


def _merge_groups(api: list[dict[str, Any]], agent: list[dict[str, Any]],
                  limit: int = 0) -> list[dict[str, Any]]:
    """API groups (cut to `limit` by spend) + agent groups. Agent rows cost 0, so
    they are appended after the cut instead of competing for it."""
    by_k = {r["k"]: {**r, "cdenom": int(r["tin"])} for r in api}
    agent_only: list[dict[str, Any]] = []
    for a in agent:
        cur = by_k.get(a["k"])
        if cur is None:
            agent_only.append(dict(a))
            continue
        for f in ("n", "tin", "cache_r", "ok_n", "cdenom"):
            cur[f] = int(cur[f]) + a[f]
        if a.get("served"):
            cur["served"] = a["served"]
    out = sorted(by_k.values(), key=lambda r: (-float(r["spend"]), -int(r["n"]), str(r["k"])))
    if limit:
        out = out[:limit]
    return out + sorted(agent_only, key=lambda r: (-int(r["n"]), str(r["k"])))


def merge_agent(d: dict[str, Any], a: dict[str, Any], api_daily: list[int]) -> dict[str, Any]:
    """Fold agent aggregates `a` into the usage breakdown `d` (in place)."""
    t = d["totals"]
    t["api_calls"] = t["calls"]
    t["calls"] += a["n"]
    t["ok_n"] += a["done"]
    t["err_n"] += a["error"]
    t["tin"] += a["tin"]
    t["tout"] += a["tout"]
    t["cache_read"] += a["cache_r"]
    done = t["ok_n"] + t["err_n"]
    t["success"] = (t["ok_n"] * 100.0 / done) if done else None
    denom = (t["tin"] - a["tin"]) + a["cdenom"]          # API input + agent denominator
    t["cache_hit"] = min(100.0, t["cache_read"] * 100.0 / denom) if denom else None
    t["agent"] = {k: a[k] for k in ("n", "done", "error", "cancelled", "active", "tin",
                                    "tout", "cache_r")}
    d["by_provider"] = _merge_groups(d["by_provider"], a["by_provider"])
    d["by_model"] = _merge_groups(d["by_model"], a["by_model"], 12)
    d["by_capability"] = _merge_groups(d["by_capability"], a["by_capability"])
    d["cap_spark"] = {**d["cap_spark"], **a["cap_spark"]}
    d["daily"] = [{"api": x, "agent": y} for x, y in zip(api_daily, a["by_bucket"], strict=False)]
    return d


async def project_breakdown_with_agents(project_id: int, start: datetime | None,
                                        end: datetime | None, bucket: str) -> dict[str, Any]:
    """`project_breakdown` (usage_log) + agent jobs + the 50 latest agent runs
    (journal rows from the shared requests query, `type=agent`)."""
    d, a, daily, runs = await q.gather(
        q.project_breakdown(project_id, start, end, bucket),
        agent_breakdown(project_id, start, end, bucket),
        _usage_by_bucket(project_id, start, end, bucket),
        rq.query_requests(rq.RequestFilter(start=start, end=end, project_id=project_id,
                                           kind="agent"), page_size=AGENT_RECENT_LIMIT))
    merge_agent(d, a, daily)
    d["agent_recent"] = runs[0]
    return d
