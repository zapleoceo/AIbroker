"""Job queue dispatcher — drains the `deep_jobs` queue with backpressure.

Submit (services/deep_jobs.submit_job) only ENQUEUES a `pending` row. This
loop is what actually runs the calls: every `_POLL_INTERVAL_S` it claims up to
`_MAX_CONCURRENCY` pending rows (atomic UPDATE … FOR UPDATE SKIP LOCKED, so the
2 uvicorn workers each run this loop and never double-claim a job), runs each
via run_chat, and writes the result back. Started once per web worker from the
app lifespan (main.py); cancelled cleanly on shutdown.

Why a drained queue instead of the old fire-and-forget `asyncio.create_task`:
  - Survives a worker restart (a deploy). A job whose worker died mid-run sits
    in `running` past the stale window and is re-queued by the next tick — the
    request isn't lost, just delayed. A few minutes of broker downtime delays
    answers; it never drops them.
  - Backpressure. A flood of submits no longer means a flood of concurrent
    provider calls — at most `_MAX_CONCURRENCY` run per worker; the rest wait
    their turn in the queue.
  - Transient no-capacity is retried. If run_chat finds no available provider
    right now (whole pool cooling), the job is re-queued with backoff instead
    of failing — it drains as capacity frees up. Capped at `_MAX_RETRIES` so a
    genuinely impossible job eventually errors rather than looping forever.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.services.deep_jobs import _finish
from aibroker.services.llm_service import run_chat

log = logging.getLogger(__name__)

_MAX_CONCURRENCY = int(os.environ.get("JOB_MAX_CONCURRENCY", "8"))
_POLL_INTERVAL_S = float(os.environ.get("JOB_POLL_INTERVAL_S", "1.0"))
_MAX_RETRIES = int(os.environ.get("JOB_MAX_RETRIES", "8"))
# A `running` row whose worker died mid-call is re-queued after this long. Must
# sit safely ABOVE the longest a live worker can hold a job: run_chat's own hard
# backstop is _DEEP_CALL_TIMEOUT_S (19 min via asyncio.wait_for), so a live
# worker always returns within ~19 min. 25 min gives a 6-min margin — a job
# still `running` past it means the worker really died, not that it's slow.
# (At 20 min the margin was ~1 min: a legit 19-min deep call could be re-queued
# and double-executed. See fix 2026-07-10.)
_STALE_RUNNING_S = 25 * 60


def _backoff_s(retry_count: int) -> int:
    """Delay before a re-queued job is eligible again: 5,10,20,… capped at 300."""
    return min(5 * (2 ** max(0, retry_count - 1)), 300)


async def _requeue_stale_running() -> None:  # pragma: no cover
    """A `running` row past the stale window means its worker died mid-call
    (a deploy/crash). Re-queue it (or error it if it's out of retries) so the
    request isn't stuck forever. Idempotent; safe to run every tick.

    Postgres-only (make_interval) — covered for real by the SKIP-LOCKED
    Postgres run: test_job_queue.py::test_requeue_stale_running_reclaims_dead_
    worker_job (skipif ON_SQLITE), invisible to the SQLite diff-cover run."""
    async with get_session() as s:
        await s.execute(
            text(
                "UPDATE deep_jobs SET "
                "  status = CASE WHEN retry_count + 1 > :max THEN 'error' ELSE 'pending' END, "
                "  error_message = CASE WHEN retry_count + 1 > :max "
                "    THEN 'gave up after repeated worker deaths mid-run' ELSE error_message END, "
                "  completed_at = CASE WHEN retry_count + 1 > :max THEN now() ELSE completed_at END, "
                "  started_at = NULL, "
                "  retry_count = retry_count + 1 "
                "WHERE status = 'running' "
                "  AND started_at < now() - make_interval(secs => :stale)"
            ),
            {"max": _MAX_RETRIES, "stale": _STALE_RUNNING_S},
        )


async def _claim_batch(limit: int) -> list[DeepJobRow]:  # pragma: no cover
    """Atomically claim up to `limit` eligible pending rows → running. FOR
    UPDATE SKIP LOCKED lets every worker's loop claim in parallel without ever
    grabbing the same job. Postgres-only (SKIP LOCKED) — covered via the
    Postgres drain_once tests in test_job_queue.py (skipif ON_SQLITE)."""
    if limit <= 0:
        return []
    async with get_session() as s:
        rows = (await s.execute(
            text(
                "UPDATE deep_jobs SET status = 'running', started_at = now() "
                "WHERE id IN ("
                "  SELECT id FROM deep_jobs "
                "  WHERE status = 'pending' AND (run_after IS NULL OR run_after < now()) "
                "  ORDER BY created_at "
                "  LIMIT :n "
                "  FOR UPDATE SKIP LOCKED"
                ") RETURNING *"
            ),
            {"n": limit},
        )).mappings().all()
    return [DeepJobRow(**dict(r)) for r in rows]


async def _requeue_or_fail(job_id: int, retry_count: int, reason: str) -> None:  # pragma: no cover
    """A job attempt didn't produce a result (no capacity, or an error). Retry
    with backoff until `_MAX_RETRIES`, then give up and mark it error. Covered
    by test_job_queue.py's Postgres tests (test_drain_once_requeues_when_no_
    provider / test_drain_once_errors_after_max_retries), skipif ON_SQLITE."""
    if retry_count + 1 > _MAX_RETRIES:
        await _finish(job_id, status="error",
                       error_message=f"{reason} (gave up after {retry_count} retries)")
        return
    async with get_session() as s:
        await s.execute(
            text(
                "UPDATE deep_jobs SET status = 'pending', started_at = NULL, "
                "  retry_count = :rc, run_after = now() + make_interval(secs => :backoff) "
                "WHERE id = :id"
            ),
            {"id": job_id, "rc": retry_count + 1, "backoff": _backoff_s(retry_count + 1)},
        )


async def _execute(row: DeepJobRow) -> None:  # pragma: no cover
    """Run one claimed job to a terminal state (done) or re-queue/fail it.
    Reached only from a real claimed row → Postgres-only; covered by
    test_job_queue.py's drain_once tests (skipif ON_SQLITE)."""
    req = row.request
    async with get_session() as s:
        project = await s.get(ProjectRow, row.project_id)
    if project is None:
        await _finish(row.id, status="error", error_message="project no longer exists")
        return
    try:
        outcome = await run_chat(
            project=project, capability=row.capability,
            messages=req["messages"], model=req.get("model"),
            max_tokens=req["max_tokens"], temperature=req["temperature"],
            response_format=req.get("response_format"), workflow=req.get("workflow"),
        )
    except Exception as e:  # noqa: BLE001 — a job must always reach a terminal/requeued state
        log.warning("job %d (%s) errored: %s", row.id, row.capability, e)
        await _requeue_or_fail(row.id, row.retry_count, f"run failed: {e}")
        return
    if outcome is None:
        # No provider available right now — retry as capacity frees up.
        await _requeue_or_fail(row.id, row.retry_count,
                                f"no provider available for {row.capability}")
        return
    await _finish(
        row.id, status="done", result_text=outcome.text,
        result_meta={
            "provider": outcome.provider, "model": outcome.model,
            "tokens_in": outcome.tokens_in, "tokens_out": outcome.tokens_out,
            "cost_usd": outcome.cost_usd, "latency_ms": outcome.latency_ms,
            "key_label": outcome.key_label, "request_id": outcome.request_id,
            "cache_read_tokens": outcome.cache_read_tokens,
            "cache_write_tokens": outcome.cache_write_tokens,
        },
        expect_started_at=row.started_at,
    )


async def drain_once(limit: int = _MAX_CONCURRENCY) -> int:  # pragma: no cover
    """One dispatch pass: re-queue stale, claim up to `limit`, run them all to
    completion. Returns how many were claimed. Used directly by tests (awaits
    every job, deterministic) and by the forever loop. Postgres-only (its claim
    uses SKIP LOCKED) — exercised by test_job_queue.py's Postgres tests and the
    async submit→drain→poll integration tests (all skipif ON_SQLITE)."""
    await _requeue_stale_running()
    claimed = await _claim_batch(limit)
    if claimed:
        await asyncio.gather(*(_execute(r) for r in claimed), return_exceptions=True)
    return len(claimed)


async def dispatcher_loop(stop: asyncio.Event) -> None:  # pragma: no cover — loop harness
    """Forever: keep up to `_MAX_CONCURRENCY` jobs in flight, filling free slots
    each tick. Runs one instance per web worker (lifespan). Never lets one slow
    job (nemotron minutes) block claiming others — each runs as its own task."""
    inflight: set[asyncio.Task[Any]] = set()
    log.info("job dispatcher started (concurrency=%d, poll=%.1fs)",
             _MAX_CONCURRENCY, _POLL_INTERVAL_S)
    while not stop.is_set():
        try:
            await _requeue_stale_running()
            free = _MAX_CONCURRENCY - len(inflight)
            if free > 0:
                for row in await _claim_batch(free):
                    t = asyncio.create_task(_execute(row))
                    inflight.add(t)
                    t.add_done_callback(inflight.discard)
        except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
            log.exception("dispatcher tick failed: %s", e)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=_POLL_INTERVAL_S)
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)
    log.info("job dispatcher stopped")
