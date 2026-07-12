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

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, text

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.db.resilience import retry_terminal_write

log = logging.getLogger(__name__)

# NOTIFY channel shared with the dispatcher's LISTEN connection (job_queue.py).
# Lives here (not job_queue) because job_queue already imports from this module.
JOBS_CHANNEL = "aib_jobs"


async def _notify_dispatcher() -> None:  # pragma: no cover — pg_notify is Postgres-only (test_submit_job_fires_pg_notify)
    async with get_session() as s:
        if s.bind.dialect.name != "postgresql":
            return
        await s.execute(text(f"SELECT pg_notify('{JOBS_CHANNEL}', '')"))


# BIGSERIAL id needs a real autoincrementing PK — SQLite doesn't do that for
# BigInteger, so submit_deep_job's insert-then-flush (and everything it
# schedules downstream) is exercised only by the Postgres-only integration
# test test_deep_submit_creates_job_and_runs_in_background, not the SQLite
# coverage run — hence `# pragma: no cover` on this and the next two defs.
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
    async with get_session() as s:
        row = DeepJobRow(project_id=project.id, capability=capability,
                          status="pending", request=request)
        s.add(row)
        await s.flush()
        job_id = row.id
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
