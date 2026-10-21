"""Client-facing job status details: error_code, queued_ms/run_ms, pending_reason/retry_at."""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent.job_status import error_code, job_timing, pending_detail
from aibroker.agent.runs import append_event, create_run, park_for_quota, requeue_run
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.routes import proxy
from aibroker.services import deep_jobs

T0 = datetime(2026, 10, 21, 10, 0, 0)


@pytest.mark.parametrize("status, cls, message, code", [
    ("error", "cli_error", "Your organization has disabled Claude subscription access", "subscription_disabled"),
    ("error", "cli_error", "Failed to refresh OAuth token: another Claude Code process", "auth_refresh_failed"),
    ("error", "cli_error", "error_during_execution", "cli_error"),
    ("error", "client_tool_protocol", "tool call expired", "tool_call_expired"),
    ("error", "client_tool_protocol", "required client tool was not called", "tool_not_called"),
    ("error", "client_tool_protocol", "terminal_stop_unconfirmed", "client_tool_protocol"),
    ("error", "quota_exhausted", "weekly limit", "quota_exhausted"),
    ("error", "worker_lost", "", "worker_restarted"),
    ("error", "codex_auth_busy", "", "capacity_busy"),
    ("error", "session_busy", "", "capacity_busy"),
    ("error", "route_disabled", "", "route_disabled"),
    ("error", None, "quota_exhausted: limit", "quota_exhausted"),         # old row: message prefix
    ("error", None, "plain failure without a class", "internal_error"),
    ("error", "Bad Class!", "x", "internal_error"),
    ("cancelled", None, "run.cancelled", "cancelled"),
    ("pending", None, None, None),
    ("done", None, None, None),
])
def test_error_code_mapping(status, cls, message, code):
    assert error_code(status, cls, message) == code


def test_job_timing_prefers_the_first_execution_start():
    first = T0 + timedelta(seconds=96)
    later_claim = T0 + timedelta(seconds=200)
    end = T0 + timedelta(seconds=230)
    assert job_timing(T0, first.isoformat(), later_claim, end, end) == (96_000, 134_000)
    assert job_timing(T0, first, None, None, end) == (96_000, 134_000)          # still running
    assert job_timing(T0, None, later_claim, end, end) == (200_000, 30_000)     # API job: row start
    assert job_timing(T0, None, None, None, T0 + timedelta(seconds=5)) == (5_000, None)  # queued
    assert job_timing(T0, "not a date", None, None, T0) == (0, None)
    assert job_timing(None, None, None, None, T0) == (None, None)
    assert job_timing(T0, None, None, T0 - timedelta(seconds=1), T0) == (None, None)  # clock skew


def test_pending_detail():
    now = T0
    soon = T0 + timedelta(minutes=5)
    assert pending_detail("pending", None, None, False, now) == ("queued", None)
    assert pending_detail("pending", soon, "waiting_quota", False, now) == ("waiting_quota", soon)
    assert pending_detail("pending", soon, "capacity_busy", False, now) == ("capacity_busy", soon)
    assert pending_detail("pending", soon, "weird", False, now) == ("queued", soon)
    assert pending_detail("pending", T0 - timedelta(seconds=1), "waiting_quota", False, now) == (
        "queued", None)                                    # the wait is over: runnable again
    assert pending_detail("running", None, None, False, now) == ("running", None)
    assert pending_detail("running", None, None, True, now) == ("tool_wait", None)
    for status in ("done", "error", "cancelled"):
        assert pending_detail(status, soon, "waiting_quota", False, now) == (None, None)


async def _job(pid: int) -> int:
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid, name=f"status_{pid}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=["llm:agent"], is_active=True, notes=""))
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:codex", prompt="p", task_id="jobs",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model="gpt-6.1-sol",
        client_job={"capability": "chat:fast", "provider": "codex-sub",
                    "model": "codex-sub/gpt-6.1-sol", "tools": [], "tool_choice": None})
    return rid


async def _poll(rid: int, pid: int):
    return proxy._job_response(await deep_jobs.get_job(rid, pid))


async def test_the_poll_reports_first_start_error_code_and_pending_reason():
    pid = 9701
    rid = await _job(pid)
    queued = await _poll(rid, pid)
    assert queued.status == "pending" and queued.pending_reason == "queued"
    assert queued.queued_ms is not None and queued.run_ms is None and queued.error_code is None

    await append_event(rid, ev.RunStarted(capability="agent:codex", account_id="owner"))
    async with get_session() as s:
        first = (await s.get(DeepJobRow, rid)).result_meta["first_started_at"]
    await requeue_run(rid, 60)                               # e.g. the Codex auth gate
    busy = await _poll(rid, pid)
    assert busy.pending_reason == "capacity_busy" and busy.retry_at is not None
    await park_for_quota(rid, datetime.utcnow() + timedelta(hours=1))
    assert (await _poll(rid, pid)).pending_reason == "waiting_quota"

    await append_event(rid, ev.RunStarted(capability="agent:codex", account_id="owner"))        # a later start keeps the first one
    async with get_session() as s:
        assert (await s.get(DeepJobRow, rid)).result_meta["first_started_at"] == first
    await append_event(rid, ev.RunFailed(error_class="cli_error",
                                         message="Your organization has disabled Claude access"))
    failed = await _poll(rid, pid)
    assert failed.status == "error" and failed.error_code == "subscription_disabled"
    assert failed.error.startswith("cli_error: ")            # free text unchanged
    assert failed.run_ms is not None and failed.queued_ms is not None
    async with get_session() as s:
        meta = (await s.get(DeepJobRow, rid)).result_meta
    assert meta["first_started_at"] == first and meta["error_class"] == "cli_error"


async def test_a_completed_job_reports_timings_and_no_error_code():
    pid = 9702
    rid = await _job(pid)
    await append_event(rid, ev.RunStarted(capability="agent:codex", account_id="owner"))
    await append_event(rid, ev.RunCompleted(final_text="ok"))
    done = await _poll(rid, pid)
    assert done.status == "done" and done.error_code is None and done.pending_reason is None
    assert done.queued_ms is not None and done.run_ms is not None
