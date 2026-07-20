"""Job queue dispatcher — backoff math + NOTIFY wake-up plumbing (SQLite) +
claim/execute/requeue loop (Postgres-only: the claim uses FOR UPDATE SKIP
LOCKED)."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.services import job_queue
from aibroker.services.job_queue import (
    _backoff_s,
    _dialect_name,
    _listen_dsn,
    _wait_stop_or_wake,
    drain_once,
)
from aibroker.services.llm_service import ChatOutcome

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


def test_backoff_s_grows_and_caps():
    """5, 10, 20, 40, … capped at 300s. Guards the exponential-with-cap math."""
    assert _backoff_s(1) == 5
    assert _backoff_s(2) == 10
    assert _backoff_s(3) == 20
    assert _backoff_s(4) == 40
    assert _backoff_s(20) == 300           # capped
    assert _backoff_s(0) == 5              # floor guard (max(0, n-1))


# ─── NOTIFY wake-up plumbing (SQLite-runnable) ───────────────────────────────


def test_listen_dsn_strips_asyncpg_driver_suffix():
    assert _listen_dsn("postgresql+asyncpg://u:p@h:5/db") == "postgresql://u:p@h:5/db"


def test_listen_dsn_plain_dsn_unchanged():
    assert _listen_dsn("postgresql://u:p@h:5/db") == "postgresql://u:p@h:5/db"


async def test_wait_stop_or_wake_returns_immediately_on_wake():
    """A set wake Event (a NOTIFY arrived) must not wait out the poll timeout."""
    stop, wake = asyncio.Event(), asyncio.Event()
    wake.set()
    await asyncio.wait_for(_wait_stop_or_wake(stop, wake, timeout=30.0), timeout=1.0)


async def test_wait_stop_or_wake_returns_immediately_on_stop():
    stop, wake = asyncio.Event(), asyncio.Event()
    stop.set()
    await asyncio.wait_for(_wait_stop_or_wake(stop, wake, timeout=30.0), timeout=1.0)


async def test_wait_stop_or_wake_times_out_as_poll_fallback():
    """Neither event set → the timed poll fallback fires (a missed NOTIFY can
    never stall the queue)."""
    stop, wake = asyncio.Event(), asyncio.Event()
    await asyncio.wait_for(_wait_stop_or_wake(stop, wake, timeout=0.01), timeout=1.0)


async def test_execute_budget_exhausted_finishes_without_burning_retries():
    """A project/global cap block (run_chat → BUDGET_EXHAUSTED) fails the job
    honestly and immediately: an accurate error_message, NO retry burn (more
    retries can't create budget), and one throttled owner alert."""
    from aibroker.services.llm_service import BUDGET_EXHAUSTED

    pid = 990202
    async with get_session() as s:
        s.add(ProjectRow(id=pid, name="cap-fixture", project_key_hash="h",
                         project_key_prefix="pk_x", allowed_scopes=["llm:chat"]))

    async def fake_run_chat(**kw):
        return BUDGET_EXHAUSTED

    with patch.object(job_queue, "run_chat", fake_run_chat), \
         patch.object(job_queue, "_finish", AsyncMock()) as finish, \
         patch.object(job_queue, "_requeue_or_fail", AsyncMock()) as requeue, \
         patch.object(job_queue, "alert", AsyncMock()) as alert_mock:
        await job_queue._execute(_unclaimed_row(pid, retry_count=0))
    requeue.assert_not_called()            # no retry burn on a spent budget
    finish.assert_awaited_once()
    assert finish.await_args.kwargs["status"] == "error"
    assert "budget cap" in finish.await_args.kwargs["error_message"]
    alert_mock.assert_awaited_once()
    assert alert_mock.await_args.args[0] == f"budget:{pid}"
    assert alert_mock.await_args.kwargs["throttle_min"] == 24 * 60


@pytest.mark.skipif(not ON_SQLITE, reason="asserts the SQLite degradation path")
async def test_dispatcher_loop_sqlite_no_listener_and_clean_stop():
    """On SQLite the LISTEN task must never start (dialect guard) and the loop
    still polls and exits cleanly on the stop event — exactly the pre-NOTIFY
    behaviour the SQLite tests rely on."""
    assert await _dialect_name() == "sqlite"
    stop = asyncio.Event()
    with patch.object(job_queue, "_listen_for_jobs") as listener, \
         patch.object(job_queue, "_requeue_stale_running", AsyncMock()), \
         patch.object(job_queue, "_claim_batch", AsyncMock(return_value=[])):
        task = asyncio.create_task(job_queue.dispatcher_loop(stop))
        await asyncio.sleep(0.05)          # let it run at least one pass
        stop.set()
        await asyncio.wait_for(task, timeout=5)
    listener.assert_not_called()


@pytest.mark.skipif(ON_SQLITE, reason="pg_notify needs Postgres")
async def test_submit_job_fires_pg_notify():
    """submit_job must NOTIFY the dispatcher channel after the enqueue commits —
    asserted end-to-end via a real LISTEN connection (the same mechanism
    _listen_for_jobs uses)."""
    import asyncpg

    from aibroker.config import get_settings
    from aibroker.services.deep_jobs import JOBS_CHANNEL, submit_job

    got = asyncio.Event()
    conn = await asyncpg.connect(_listen_dsn(get_settings().DATABASE_URL))
    try:
        await conn.add_listener(JOBS_CHANNEL, lambda *_: got.set())
        pid = await _make_project(["llm:chat"])
        async with get_session() as s:
            project = await s.get(ProjectRow, pid)
        await submit_job(
            project=project, capability="chat:fast",
            messages=[{"role": "user", "content": "hi"}], model=None,
            max_tokens=64, temperature=0.7, response_format=None, workflow="t",
        )
        await asyncio.wait_for(got.wait(), timeout=5)
    finally:
        await conn.close()


async def _make_project(scopes: list[str]) -> int:
    async with get_session() as s:
        p = ProjectRow(
            name=f"jobq-{os.urandom(4).hex()}", project_key_hash="h",
            project_key_prefix="pk_x", allowed_scopes=scopes,
        )
        s.add(p)
        await s.flush()
        return p.id


async def _enqueue(project_id: int, capability: str = "chat:fast") -> int:
    async with get_session() as s:
        row = DeepJobRow(
            project_id=project_id, capability=capability, status="pending",
            request={"messages": [{"role": "user", "content": "hi"}], "model": None,
                     "max_tokens": 64, "temperature": 0.7, "response_format": None,
                     "workflow": "t"},
        )
        s.add(row)
        await s.flush()
        return row.id


# ─── final-retry paid escalation (SQLite-runnable: drives _execute directly) ─


def _unclaimed_row(pid: int, retry_count: int) -> DeepJobRow:
    """In-memory row (never persisted — SQLite can't autoincrement the
    BigInteger PK); _execute only reads its attributes, and run_chat /
    _requeue_or_fail are mocked so no job UPDATE is ever attempted."""
    return DeepJobRow(
        id=1, project_id=pid, capability="chat:smart", status="running",
        retry_count=retry_count,
        request={"messages": [{"role": "user", "content": "hi"}], "model": None,
                 "max_tokens": 64, "temperature": 0.7, "response_format": None,
                 "workflow": "t"},
    )


async def test_execute_final_attempt_escalates_to_paid_only():
    """The LAST retry before give-up must call run_chat with paid_only=True —
    the guaranteed-answer mechanism (2026-07-16: 148 jobs/h died 'no provider
    available' in a free-pool storm while the paid deepseek tail was healthy).
    A non-final row stays paid_only=False."""
    pid = 990101  # explicit id — SQLite can't autoincrement the BigInteger PK
    async with get_session() as s:
        s.add(ProjectRow(id=pid, name="paid-only-fixture", project_key_hash="h",
                         project_key_prefix="pk_x", allowed_scopes=["llm:chat"]))
    seen: list[bool] = []

    async def fake_run_chat(**kw):
        # Returns None: no paid capacity either → normal requeue/give-up path.
        seen.append(kw["paid_only"])

    with patch.object(job_queue, "run_chat", fake_run_chat), \
         patch.object(job_queue, "_requeue_or_fail", AsyncMock()) as requeue:
        await job_queue._execute(
            _unclaimed_row(pid, retry_count=job_queue._MAX_RETRIES - 1))
        await job_queue._execute(_unclaimed_row(pid, retry_count=0))
    assert seen == [True, False]
    assert requeue.await_count == 2  # outcome None still requeues/fails honestly


async def test_execute_no_paid_escalation_when_chain_has_no_paid_tail():
    """A capability whose chain never reaches a paid provider (chat:deep is
    nvidia-only) must NOT escalate to paid_only on the final retry — that would
    be a guaranteed no-op. The last shot stays a normal free-lane walk."""
    pid = 990303
    async with get_session() as s:
        s.add(ProjectRow(id=pid, name="deep-fixture", project_key_hash="h",
                         project_key_prefix="pk_x", allowed_scopes=["llm:deep"]))
    seen: list[bool] = []

    async def fake_run_chat(**kw):
        seen.append(kw["paid_only"])

    row = DeepJobRow(
        id=1, project_id=pid, capability="chat:deep", status="running",
        retry_count=job_queue._MAX_RETRIES - 1,
        request={"messages": [{"role": "user", "content": "hi"}], "model": None,
                 "max_tokens": 64, "temperature": 0.7, "response_format": None,
                 "workflow": "t"},
    )
    with patch.object(job_queue, "run_chat", fake_run_chat), \
         patch.object(job_queue, "_requeue_or_fail", AsyncMock()):
        await job_queue._execute(row)
    assert seen == [False]      # final retry, but no paid tail → normal walk


@pytest.mark.skipif(ON_SQLITE, reason="claim uses FOR UPDATE SKIP LOCKED — Postgres only")
async def test_drain_once_runs_pending_job_to_done():
    pid = await _make_project(["llm:chat"])
    jid = await _enqueue(pid)
    outcome = ChatOutcome(
        text="answer", provider="cerebras", model="cerebras/gpt-oss-120b",
        tokens_in=5, tokens_out=2, cost_usd=0.0, latency_ms=100,
        key_label="k", request_id=42,
    )
    with patch("aibroker.services.job_queue.run_chat", AsyncMock(return_value=outcome)):
        claimed = await drain_once()
    assert claimed == 1
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "done"
        assert row.result_text == "answer"
        assert row.result_meta["request_id"] == 42


@pytest.mark.skipif(ON_SQLITE, reason="claim uses FOR UPDATE SKIP LOCKED — Postgres only")
async def test_drain_once_requeues_when_no_provider():
    """run_chat → None (no capacity) re-queues with backoff, not error — the
    job drains as capacity frees up."""
    pid = await _make_project(["llm:chat"])
    jid = await _enqueue(pid)
    with patch("aibroker.services.job_queue.run_chat", AsyncMock(return_value=None)):
        await drain_once()
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "pending"        # back in the queue, not failed
        assert row.retry_count == 1
        assert row.run_after is not None       # backoff scheduled


@pytest.mark.skipif(ON_SQLITE, reason="claim uses FOR UPDATE SKIP LOCKED — Postgres only")
async def test_drain_once_errors_after_max_retries():
    """A job already at the retry cap fails instead of looping forever."""
    pid = await _make_project(["llm:chat"])
    async with get_session() as s:
        row = DeepJobRow(
            project_id=pid, capability="chat:fast", status="pending",
            retry_count=job_queue._MAX_RETRIES,
            request={"messages": [{"role": "user", "content": "x"}], "model": None,
                     "max_tokens": 64, "temperature": 0.7, "response_format": None,
                     "workflow": "t"},
        )
        s.add(row)
        await s.flush()
        jid = row.id
    with patch("aibroker.services.job_queue.run_chat", AsyncMock(return_value=None)):
        await drain_once()
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "error"
        assert "gave up" in row.error_message


@pytest.mark.skipif(ON_SQLITE, reason="make_interval / SKIP LOCKED — Postgres only")
async def test_requeue_stale_running_reclaims_dead_worker_job():
    """A row stuck 'running' past the stale window (its worker died mid-run) is
    put back to 'pending' so another worker re-runs it — survives a deploy."""
    from sqlalchemy import text

    pid = await _make_project(["llm:chat"])
    jid = await _enqueue(pid)
    # Force it into a stale running state (started long ago).
    async with get_session() as s:
        await s.execute(
            text("UPDATE deep_jobs SET status='running', "
                 "started_at = now() - make_interval(secs => :old) WHERE id = :id"),
            {"old": job_queue._STALE_RUNNING_S + 60, "id": jid},
        )
    await job_queue._requeue_stale_running()
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "pending"
        assert row.started_at is None
        assert row.retry_count == 1


@pytest.mark.skipif(ON_SQLITE, reason="claim uses FOR UPDATE SKIP LOCKED — Postgres only")
async def test_drain_once_requeues_when_run_chat_raises():
    """REGRESSION (2026-07-10): an unexpected exception from run_chat must not
    kill the job — it re-queues (retry_count+1) so the queue's 'always reaches a
    terminal or requeued state' guarantee holds even on a crash, not just on a
    clean None."""
    pid = await _make_project(["llm:chat"])
    jid = await _enqueue(pid)
    with patch("aibroker.services.job_queue.run_chat",
               AsyncMock(side_effect=RuntimeError("boom"))):
        assert await drain_once() == 1
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "pending"
        assert row.retry_count == 1
        assert row.started_at is None


@pytest.mark.skipif(ON_SQLITE, reason="cross-session claim state needs Postgres")
async def test_finish_claim_guard_ignores_stale_worker():
    """REGRESSION (2026-07-10): a stale worker returning late must not clobber a
    job another worker has since re-claimed. _finish with a mismatched
    expect_started_at is a no-op; with the matching token it writes."""
    from datetime import datetime

    from sqlalchemy import text

    from aibroker.services.deep_jobs import _finish

    pid = await _make_project(["llm:chat"])
    jid = await _enqueue(pid)
    live_started = datetime(2026, 6, 1, 12, 0, 0)
    async with get_session() as s:
        await s.execute(
            text("UPDATE deep_jobs SET status='running', started_at=:t WHERE id=:id"),
            {"t": live_started, "id": jid},
        )
    # Stale worker (claimed with an OLDER started_at) tries to finish → ignored.
    await _finish(jid, status="done", result_text="STALE",
                  expect_started_at=datetime(2026, 6, 1, 11, 0, 0))
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "running"
        assert row.result_text is None
    # The live claim (matching token) writes.
    await _finish(jid, status="done", result_text="LIVE",
                  expect_started_at=live_started)
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "done"
        assert row.result_text == "LIVE"


def test_deep_wall_deadline_stays_under_stale_reclaim():
    """chat:deep must stop STARTING attempts early enough that its last ~19min
    call still lands before the 25min stale-reclaim — otherwise a reclaimed row
    is double-executed (double nvidia spend). Guards the coupled invariant
    across llm_service + job_queue (2026-07-19 review)."""
    from aibroker.services.job_queue import _STALE_RUNNING_S
    from aibroker.services.llm_service import (
        _DEEP_CALL_TIMEOUT_S,
        _DEEP_WALL_DEADLINE_S,
    )

    assert _DEEP_WALL_DEADLINE_S + _DEEP_CALL_TIMEOUT_S < _STALE_RUNNING_S


@pytest.mark.skipif(ON_SQLITE, reason="cross-session claim state needs Postgres")
async def test_requeue_or_fail_claim_guard_ignores_stale_worker():
    """2026-07-19: the error/no-provider requeue path is now claim-guarded like
    _finish's success path. A stale worker whose job was re-claimed must NOT
    reset the live claim (started_at→NULL) — that would let a third worker
    double-execute. Only the matching claim token requeues."""
    from datetime import datetime

    from sqlalchemy import text

    from aibroker.services.job_queue import _requeue_or_fail

    pid = await _make_project(["llm:chat"])
    jid = await _enqueue(pid)
    live_started = datetime(2026, 6, 1, 12, 0, 0)
    async with get_session() as s:
        await s.execute(
            text("UPDATE deep_jobs SET status='running', started_at=:t, retry_count=0 "
                 "WHERE id=:id"),
            {"t": live_started, "id": jid})
    # Stale worker (older claim token) → guarded no-op.
    await _requeue_or_fail(jid, 0, "boom",
                           expect_started_at=datetime(2026, 6, 1, 11, 0, 0))
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "running"
        assert row.started_at == live_started
    # Live claim (matching token) → requeues.
    await _requeue_or_fail(jid, 0, "boom", expect_started_at=live_started)
    async with get_session() as s:
        row = await s.get(DeepJobRow, jid)
        assert row.status == "pending"
        assert row.started_at is None


@pytest.mark.skipif(ON_SQLITE, reason="claim uses FOR UPDATE SKIP LOCKED — Postgres only")
async def test_claim_batch_concurrent_workers_never_double_claim():
    """Two 'workers' claiming concurrently (the real 2-uvicorn-worker setup)
    must partition the pending set — a job claimed by both would be executed
    and billed twice. SKIP LOCKED is the guarantee; this is its only test
    under actual contention."""
    import asyncio

    from aibroker.services.job_queue import _claim_batch

    pid = await _make_project(["llm:chat"])
    ids = [await _enqueue(pid) for _ in range(6)]

    async def worker() -> list[int]:
        rows = await _claim_batch(limit=6)
        return [r.id for r in rows]

    a, b = await asyncio.gather(worker(), worker())
    assert set(a) | set(b) <= set(ids)
    assert not (set(a) & set(b)), f"double-claimed jobs: {set(a) & set(b)}"
    assert len(a) + len(b) == 6  # nothing lost either
