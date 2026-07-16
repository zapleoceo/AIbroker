"""Job queue dispatcher — drains the `deep_jobs` queue with backpressure.

Submit (services/deep_jobs.submit_job) only ENQUEUES a `pending` row. This
loop is what actually runs the calls: woken instantly by submit's NOTIFY (or
by the `_IDLE_POLL_INTERVAL_S` fallback poll; plain `_POLL_INTERVAL_S` polling
on SQLite), it claims up to `_MAX_CONCURRENCY` pending rows (atomic UPDATE …
FOR UPDATE SKIP LOCKED, so the 2 uvicorn workers each run this loop and never
double-claim a job), runs each via run_chat, and writes the result back.
Started once per web worker from the app lifespan (main.py); cancelled cleanly
on shutdown.

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

import asyncpg
from sqlalchemy import text

from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.services.deep_jobs import JOBS_CHANNEL, _finish
from aibroker.services.llm_service import BUDGET_EXHAUSTED, run_chat
from aibroker.telemetry.notifier import alert

log = logging.getLogger(__name__)

_MAX_CONCURRENCY = int(os.environ.get("JOB_MAX_CONCURRENCY", "8"))
_POLL_INTERVAL_S = float(os.environ.get("JOB_POLL_INTERVAL_S", "1.0"))
# With the LISTEN/NOTIFY wake-up (Postgres) the timed poll is only a fallback
# for a missed NOTIFY, so it can be lazier than the old 1s hot poll.
_IDLE_POLL_INTERVAL_S = 5.0
_LISTEN_RECONNECT_MAX_S = 30.0
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
    Reached only from a real claimed row → Postgres-only via drain_once tests
    (skipif ON_SQLITE); the paid-only escalation branch is also driven directly
    on SQLite (test_execute_final_attempt_escalates_to_paid_only)."""
    req = row.request
    async with get_session() as s:
        project = await s.get(ProjectRow, row.project_id)
    if project is None:
        await _finish(row.id, status="error", error_message="project no longer exists")
        return
    # Final attempt before give-up walks the paid tail only: free-pool storms
    # outlast the 8-retry window, so the last shot must not waste itself on a
    # cooling free pool (2026-07-16: 148 jobs/h died "no provider available"
    # while the paid deepseek tail was healthy the whole time).
    paid_only = row.retry_count >= _MAX_RETRIES - 1
    if paid_only:
        log.info("final retry — paid tail only, job %d", row.id)
    try:
        outcome = await run_chat(
            project=project, capability=row.capability,
            messages=req["messages"], model=req.get("model"),
            max_tokens=req["max_tokens"], temperature=req["temperature"],
            response_format=req.get("response_format"), workflow=req.get("workflow"),
            paid_only=paid_only,
        )
    except Exception as e:  # noqa: BLE001 — a job must always reach a terminal/requeued state
        log.warning("job %d (%s) errored: %s", row.id, row.capability, e)
        await _requeue_or_fail(row.id, row.retry_count, f"run failed: {e}")
        return
    if outcome is BUDGET_EXHAUSTED:
        # A project/global daily cap is spent — more retries can't create budget,
        # so give up immediately with an honest message (not "no provider
        # available"). Alert the owner once per day so a silently-capped project
        # is visible, not just invisibly stalled until 00:00 UTC.
        await _finish(row.id, status="error",
                       error_message="daily budget cap reached — retry after 00:00 UTC")
        await alert(f"budget:{row.project_id}",
                    f"project <b>{project.name}</b> hit its daily budget cap — "
                    "jobs paused until 00:00 UTC", throttle_min=24 * 60)
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


def _listen_dsn(database_url: str) -> str:
    """SQLAlchemy URL → plain asyncpg DSN: strip the '+asyncpg' driver suffix
    ('postgresql+asyncpg://…' → 'postgresql://…') — asyncpg.connect doesn't
    understand SQLAlchemy driver qualifiers."""
    scheme, sep, rest = database_url.partition("://")
    return f"{scheme.partition('+')[0]}{sep}{rest}"


async def _dialect_name() -> str:
    async with get_session() as s:
        return s.bind.dialect.name


async def _wait_stop_or_wake(stop: asyncio.Event, wake: asyncio.Event,
                             timeout: float) -> None:
    """Sleep until shutdown, a NOTIFY wake-up, or the poll-fallback timeout —
    whichever comes first."""
    waiters = [asyncio.create_task(stop.wait()), asyncio.create_task(wake.wait())]
    try:
        await asyncio.wait(waiters, timeout=timeout,
                           return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def _listen_for_jobs(wake: asyncio.Event, stop: asyncio.Event) -> None:  # pragma: no cover — needs a live Postgres
    """Dedicated LISTEN connection that sets `wake` on every submit_job NOTIFY.

    Raw asyncpg (not the SQLAlchemy engine): add_listener needs a connection
    held open outside the pool, which `engine.raw_connection()` can't provide
    cleanly. Reconnects with backoff on loss; while it's down the dispatcher's
    timed poll keeps draining (fail-open), so a dead listener only costs
    latency, never jobs."""
    # direct_database_url: LISTEN must bypass PgBouncer (transaction pooling
    # detaches the server backend between transactions, dropping the
    # subscription silently — the poll fallback would mask it as latency).
    dsn = _listen_dsn(get_settings().direct_database_url)
    backoff = 1.0
    while not stop.is_set():
        try:
            conn = await asyncpg.connect(dsn)
            try:
                await conn.add_listener(JOBS_CHANNEL, lambda *_: wake.set())
                backoff = 1.0
                # A dropped connection goes silent (no error surfaces here),
                # so is_closed() is re-checked on a slow cadence.
                while not stop.is_set() and not conn.is_closed():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(),
                                               timeout=_LISTEN_RECONNECT_MAX_S)
            finally:
                with contextlib.suppress(Exception):
                    await conn.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the listener must outlive any connection error
            log.warning("job NOTIFY listener down (%s) — poll fallback active, "
                        "reconnect in %.0fs", e, backoff)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            backoff = min(backoff * 2, _LISTEN_RECONNECT_MAX_S)


async def dispatcher_loop(stop: asyncio.Event) -> None:  # pragma: no cover — loop harness
    """Forever: keep up to `_MAX_CONCURRENCY` jobs in flight, filling free slots
    each tick. Runs one instance per web worker (lifespan). Never lets one slow
    job (nemotron minutes) block claiming others — each runs as its own task.

    Hybrid wake-up (2026-07-12): on Postgres a LISTEN connection sets `wake`
    the instant submit_job NOTIFYs, killing the old up-to-1s claim-latency
    floor; the timed wait stays as a fallback (`_IDLE_POLL_INTERVAL_S`) so a
    missed NOTIFY can never stall jobs. On SQLite (tests) no listener starts
    and the loop degrades to the plain `_POLL_INTERVAL_S` poll, as before."""
    inflight: set[asyncio.Task[Any]] = set()
    wake = asyncio.Event()
    listener: asyncio.Task[None] | None = None
    if await _dialect_name() == "postgresql":
        listener = asyncio.create_task(_listen_for_jobs(wake, stop))
    idle_timeout = _IDLE_POLL_INTERVAL_S if listener is not None else _POLL_INTERVAL_S
    log.info("job dispatcher started (concurrency=%d, idle poll=%.1fs, notify=%s)",
             _MAX_CONCURRENCY, idle_timeout, listener is not None)
    while not stop.is_set():
        # Clear BEFORE claiming: a NOTIFY landing mid-pass re-sets it, so the
        # wait below returns immediately instead of losing that wake-up.
        wake.clear()
        claimed = 0
        try:
            await _requeue_stale_running()
            free = _MAX_CONCURRENCY - len(inflight)
            if free > 0:
                for row in await _claim_batch(free):
                    claimed += 1
                    t = asyncio.create_task(_execute(row))
                    inflight.add(t)
                    t.add_done_callback(inflight.discard)
        except Exception as e:  # noqa: BLE001 — a bad tick must not kill the loop
            log.exception("dispatcher tick failed: %s", e)
        if listener is not None and claimed:
            continue  # queue still hot — keep draining without waiting
        await _wait_stop_or_wake(stop, wake, idle_timeout)
    if listener is not None:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)
    log.info("job dispatcher stopped")
