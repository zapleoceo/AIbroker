"""Job queue dispatcher — backoff math (SQLite) + claim/execute/requeue loop
(Postgres-only: the claim uses FOR UPDATE SKIP LOCKED)."""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.services import job_queue
from aibroker.services.job_queue import _backoff_s, drain_once
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
