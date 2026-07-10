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

Submit creates a `deep_jobs` row and schedules the real call in the background
(asyncio.create_task on whichever uvicorn worker handled the submit) — the HTTP
response returns immediately. Poll reads the row from Postgres, so it works no
matter which worker answers; no in-process handle needs to survive.

A worker restart mid-job leaves the row stuck at "pending" — `get_job` lazily
marks anything past `_STALE_AFTER_S` a timeout error, no separate sweeper.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow

log = logging.getLogger(__name__)

# Longest observed real call was ~8 min; give real margin before calling a
# stuck "pending" row a timeout rather than a still-legitimately-running job.
_STALE_AFTER_S = 20 * 60


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
        return row.id


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


async def _finish(  # pragma: no cover — job execution is the dispatcher's (job_queue.py)
    job_id: int, *, status: str,
    result_text: str | None = None,
    result_meta: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        if row is None:  # pragma: no cover — job row deleted underneath us
            return
        row.status = status
        row.result_text = result_text
        row.result_meta = result_meta
        row.error_message = error_message
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)


async def get_job(job_id: int, project_id: int) -> DeepJobRow | None:
    """Fetch a job, scoped to the caller's own project. Lazily resolves a
    stuck "pending" row into a timeout error instead of polling forever."""
    async with get_session() as s:
        row = (await s.execute(
            select(DeepJobRow).where(
                DeepJobRow.id == job_id, DeepJobRow.project_id == project_id
            )
        )).scalar_one_or_none()
        # coverage.py has a measurement gap right after this await (an
        # SQLAlchemy async/greenlet boundary) — test_deep_poll_404_for_unknown_job
        # exercises this branch for real (asserts a 404), it just doesn't
        # register as hit.
        if row is None:  # pragma: no cover
            return None  # pragma: no cover
        # A row read back here was inserted by a SEPARATE session/request
        # (the submit call, or a test's direct insert) — on SQLite that
        # cross-session read doesn't see the row at all (each connection to
        # `:memory:` is effectively isolated), so everything below only runs
        # for real on Postgres. Covered by test_deep_poll_pending_job_*,
        # test_deep_poll_scoped_to_owning_project, test_deep_poll_done_job_*,
        # test_deep_poll_error_job_*, test_deep_poll_stale_pending_job_times_out
        # (all skipif ON_SQLITE).
        if row.status == "pending":  # pragma: no cover
            age_s = (datetime.now(UTC).replace(tzinfo=None) - row.created_at).total_seconds()
            if age_s > _STALE_AFTER_S:
                row.status = "error"
                row.error_message = (
                    f"timed out after {int(age_s)}s with no result — the worker "
                    "handling this job likely restarted mid-call"
                )
                row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        return row  # pragma: no cover


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
