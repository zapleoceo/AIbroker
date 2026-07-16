"""Async jobs (submit + poll) — generic over any chat capability.

Why this exists: originally for chat:deep — nemotron-3-ultra has been observed
taking up to ~8 minutes on NVIDIA's free pool, past Cloudflare's (~100s) and
this broker's nginx (proxy_read_timeout 120s) read timeouts, so a sync HTTP
response can't carry the result: the client 504s while the broker is still
waiting and later logs a perfectly good "ok" nobody's left to see.

The same submit/poll shape now serves EVERY chat capability (POST
/v1/jobs?capability=X, roadmap Phase 4): a client can migrate off the sync
endpoint at its own pace and get a guaranteed answer (exhaustive rotation, no
held connection). Sync endpoints stay — this is additive, backward-compatible.
`submit_deep_job` is a thin wrapper over `submit_job(capability="chat:deep")`.

Submit only ENQUEUES a `pending` row and returns its id immediately; the
dispatcher loop (services/job_queue.py) claims and runs it. Poll reads the row
from Postgres, so it works no matter which worker answers; no in-process handle
needs to survive. All lifecycle transitions (running → done/error, requeue,
give-up after retries) are owned by the dispatcher — `get_job` is a pure read.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.db.resilience import retry_terminal_write

log = logging.getLogger(__name__)

# NOTIFY channel shared with the dispatcher's LISTEN connection (job_queue.py).
# Lives here (not job_queue) because job_queue already imports from this module.
JOBS_CHANNEL = "aib_jobs"

# In-flight dedup window. Measured on prod (2026-07-16): one client resubmitted
# the SAME vision payload up to 33x (480 jobs/24h vs 156 distinct payloads);
# with the dispatcher's own up-to-8 retries that's ~260 provider attempts for
# one image. 30 min is wide enough to swallow that whole resubmit storm, narrow
# enough that a genuinely repeated question tomorrow gets a fresh answer. Only
# pending/running jobs dedup — a done/error job never does: after a failure the
# client may legitimately want a retry.
_DEDUP_WINDOW_S = 30 * 60

# Flips False the first time the dedup lookup hits a missing payload_hash
# column (code deployed before migration 010 was applied) — degrade to plain
# duplicate-tolerant inserts instead of 500ing every submit. Warn once.
_dedup_available = True


def payload_hash(project_id: int, capability: str, request: dict[str, Any]) -> str:
    """md5 over project + capability + canonical (sorted, compact) request JSON."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(f"{project_id}:{capability}:{canonical}".encode()).hexdigest()


async def _find_inflight_duplicate(
    project_id: int, capability: str, phash: str
) -> int | None:
    """id of an identical pending/running job inside the dedup window, or None.

    Best-effort, not a uniqueness constraint: two truly simultaneous identical
    submits can still both insert — fine, the target is the serial resubmit
    storm, not a race window of milliseconds."""
    global _dedup_available
    if not _dedup_available:
        return None
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=_DEDUP_WINDOW_S)
    try:
        async with get_session() as s:
            return (await s.execute(
                select(DeepJobRow.id)
                .where(
                    DeepJobRow.project_id == project_id,
                    DeepJobRow.capability == capability,
                    DeepJobRow.payload_hash == phash,
                    DeepJobRow.status.in_(("pending", "running")),
                    DeepJobRow.created_at > cutoff,
                )
                .order_by(DeepJobRow.created_at.desc())
                .limit(1)
            )).scalars().first()
    except (ProgrammingError, OperationalError) as e:
        # Missing column = migration 010 not applied yet. The deploy must not
        # 500 over that — disable dedup until the operator runs the migration.
        _dedup_available = False
        log.warning(
            "job dedup disabled — payload_hash lookup failed (%s); apply "
            "infra/sql/migrations/010_deep_jobs_payload_hash.sql and RESTART "
            "(dedup stays off until process restart)", e,
        )
        return None


async def _notify_dispatcher() -> None:  # pragma: no cover — pg_notify is Postgres-only (test_submit_job_fires_pg_notify)
    async with get_session() as s:
        if s.bind.dialect.name != "postgresql":
            return
        await s.execute(text(f"SELECT pg_notify('{JOBS_CHANNEL}', '')"))


# BIGSERIAL id needs a real autoincrementing PK — SQLite doesn't do that for
# BigInteger, so submit_job's insert-then-flush (and everything it schedules
# downstream) is exercised only by the Postgres-only tests: the submit/dedup
# tests in tests/test_deep_jobs.py (test_submit_same_payload_twice_returns_
# same_id_single_row and friends) plus the end-to-end
# test_deep_submit_enqueues_and_dispatcher_drains_to_done in
# tests/test_routes_proxy.py — not the SQLite coverage run, hence
# `# pragma: no cover` on this and the next two defs.
async def submit_job(  # pragma: no cover
    *,
    project: ProjectRow,
    capability: str,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    response_format: dict[str, Any] | None,
    workflow: str | None,
) -> int:
    """ENQUEUE a job for any chat `capability` and return its id immediately.

    Submit no longer runs the call — it only inserts a `pending` row. The
    dispatcher loop (services/job_queue.py) claims and drains pending rows with
    bounded concurrency. This is what makes the queue survive a worker restart
    (a deploy) and apply backpressure: the request is durably enqueued the
    moment submit returns, whatever the broker/provider pool is doing."""
    request = {
        "messages": messages, "model": model,
        "max_tokens": max_tokens, "temperature": temperature,
        "response_format": response_format, "workflow": workflow,
    }
    phash = payload_hash(project.id, capability, request)
    existing = await _find_inflight_duplicate(project.id, capability, phash)
    if existing is not None:
        # Identical request already in flight — hand back its id (the client's
        # own resubmit storm now polls ONE job) and don't wake the dispatcher.
        log.info("dedup: project=%s %s resubmitted in-flight job %s — returning it",
                 project.id, capability, existing)
        return existing
    if _dedup_available:
        async with get_session() as s:
            row = DeepJobRow(project_id=project.id, capability=capability,
                              status="pending", request=request,
                              payload_hash=phash)
            s.add(row)
            await s.flush()
            job_id = row.id
    else:
        # Degraded (migration 010 not applied yet): raw INSERT naming ONLY the
        # pre-010 columns. An ORM insert is unsafe here — SQLAlchemy's compiled
        # cache may reuse a statement that references payload_hash from before
        # the flag flipped (bit us in CI, 2026-07-16).
        import json as _json
        async with get_session() as s:
            # retry_count named explicitly: its 0-default is Python-side (ORM),
            # so a raw INSERT can't rely on it existing in the DDL.
            job_id = (await s.execute(text(
                "INSERT INTO deep_jobs "
                "(project_id, capability, status, request, retry_count) "
                "VALUES (:p, :c, 'pending', :r, 0) RETURNING id"
            ), {"p": project.id, "c": capability,
                "r": _json.dumps(request)})).scalar_one()
    # After the enqueue COMMITS: wake the dispatcher instantly via NOTIFY so an
    # interactive chat call doesn't eat up to a full poll interval of claim
    # latency. Best-effort — the dispatcher's timed poll is the guaranteed
    # fallback, so a failed NOTIFY can delay a job but never lose it.
    try:
        await _notify_dispatcher()
    except Exception as e:  # noqa: BLE001 — never fail a durable enqueue over a wake-up hint
        log.debug("pg_notify(%s) failed: %s — poll fallback covers it",
                  JOBS_CHANNEL, e)
    return job_id


async def submit_deep_job(  # pragma: no cover
    *,
    project: ProjectRow,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    workflow: str | None,
) -> int:
    """Backward-compatible chat:deep wrapper (POST /v1/deep). nemotron isn't
    JSON-reliable, so response_format is always None here — see chains.py."""
    return await submit_job(
        project=project, capability="chat:deep", messages=messages, model=model,
        max_tokens=max_tokens, temperature=temperature, response_format=None,
        workflow=workflow,
    )


@retry_terminal_write
async def _finish(  # pragma: no cover — job execution is the dispatcher's (job_queue.py)
    job_id: int, *, status: str,
    result_text: str | None = None,
    result_meta: dict[str, Any] | None = None,
    error_message: str | None = None,
    expect_started_at: datetime | None = None,
) -> None:
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        if row is None:  # pragma: no cover — job row deleted underneath us
            return
        # Claim guard: if the caller passes the started_at it claimed the job
        # with and the row now holds a different value, the job was re-queued
        # (started_at→NULL) and re-claimed by another worker — this is a stale
        # worker returning late; don't clobber the live claim's result.
        if expect_started_at is not None and row.started_at != expect_started_at:
            return
        row.status = status
        row.result_text = result_text
        row.result_meta = result_meta
        row.error_message = error_message
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)


async def get_job(job_id: int, project_id: int) -> DeepJobRow | None:
    """Fetch a job, scoped to the caller's own project. Pure read — every
    lifecycle transition (running → done/error, requeue, give-up after
    retries) is owned by the dispatcher (services/job_queue.py). It used to
    lazily flip a stale `pending` row to error, but that raced the dispatcher
    and prematurely failed jobs still legitimately retrying under backoff."""
    async with get_session() as s:
        return (await s.execute(  # pragma: no cover
            select(DeepJobRow).where(
                DeepJobRow.id == job_id, DeepJobRow.project_id == project_id
            )
        )).scalar_one_or_none()


def next_poll_after_s(created_at: datetime) -> int:
    """Suggested poll interval — start slow (this is a minutes-long job, not
    a fast one), don't hammer the endpoint. Widens with a small cap so a
    long-pending job doesn't get polled every second nor once a minute."""
    age = (datetime.now(UTC).replace(tzinfo=None) - created_at)
    if age < timedelta(seconds=30):
        return 5
    if age < timedelta(minutes=2):
        return 10
    return 20
