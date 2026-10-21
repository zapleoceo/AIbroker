"""Job priority (migration 023): interactive jobs are claimed before batch ones, and a worker keeps
reserved slots for interactive work. SQLite-safe: the Postgres-only dispatcher SQL is checked as text."""
from __future__ import annotations

import asyncio
import inspect
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Response
from pydantic import ValidationError
from sqlalchemy import select

from aibroker.agent import worker
from aibroker.agent.runs import create_run
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow
from aibroker.routes import proxy
from aibroker.services import job_queue
from aibroker.services.deep_jobs import (
    INTERACTIVE_WORKFLOW_LABEL,
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    SubmitOutcome,
    get_job,
    payload_hash,
    priority_name,
    resolve_priority,
)
from tests.test_agent_worker import _pending, _project, _settings
from tests.test_init_sql_mirrors_migrations import _init_columns, _migration_columns

CAP = "agent:codex"


def _ctx() -> SimpleNamespace:
    scopes = ("llm:chat", "llm:agent")
    return SimpleNamespace(project=SimpleNamespace(id=11, allowed_scopes=list(scopes)),
                           has_scope=lambda scope: scope in scopes)


# --- mapping -------------------------------------------------------------------------------

@pytest.mark.parametrize("requested, labels, expected", [
    (None, None, PRIORITY_BATCH),
    (None, {"workflow": "exam"}, PRIORITY_BATCH),
    (None, {"workflow": INTERACTIVE_WORKFLOW_LABEL}, PRIORITY_INTERACTIVE),
    ("interactive", None, PRIORITY_INTERACTIVE),
    ("batch", None, PRIORITY_BATCH),
    ("batch", {"workflow": INTERACTIVE_WORKFLOW_LABEL}, PRIORITY_BATCH),   # explicit wins
    ("interactive", {"workflow": "exam"}, PRIORITY_INTERACTIVE),
])
def test_resolve_priority(requested, labels, expected):
    assert resolve_priority(requested, labels) == expected


def test_priority_codes_and_names():
    assert (PRIORITY_INTERACTIVE, PRIORITY_BATCH) == (0, 1)
    assert [priority_name(0), priority_name(1), priority_name(None)] == [
        "interactive", "batch", "batch"]


def test_chat_request_priority_is_validated():
    msgs = [{"role": "user", "content": "hi"}]
    assert proxy.ChatRequest(messages=msgs).priority is None
    assert proxy.ChatRequest(messages=msgs, priority="interactive").priority == "interactive"
    with pytest.raises(ValidationError):
        proxy.ChatRequest(messages=msgs, priority="urgent")


def test_priority_is_not_part_of_the_payload_hash():
    msgs = [{"role": "user", "content": "hi"}]
    hashes = {payload_hash(11, "chat:fast", proxy._client_request(
        proxy.ChatRequest(messages=msgs, priority=p))) for p in (None, "interactive", "batch")}
    assert len(hashes) == 1
    assert "priority" not in proxy._client_request(proxy.ChatRequest(messages=msgs, priority="batch"))


# --- submit paths pass the priority on ------------------------------------------------------

@pytest.mark.parametrize("body_extra, expected", [
    ({}, 1),
    ({"priority": "interactive"}, 0),
    ({"labels": {"workflow": INTERACTIVE_WORKFLOW_LABEL}}, 0),
    ({"priority": "batch", "labels": {"workflow": INTERACTIVE_WORKFLOW_LABEL}}, 1),
])
async def test_subscription_submit_passes_priority_to_create_run(monkeypatch, body_extra, expected):
    create = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    await proxy.jobs_submit(
        proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="hi")],
                          model="claude-sub/claude-sonnet-5-5", **body_extra),
        Response(), capability="chat:fast", ctx=_ctx())
    assert create.await_args.kwargs["priority"] == expected


@pytest.mark.parametrize("body_extra, expected", [
    ({}, 1), ({"priority": "interactive"}, 0),
    ({"labels": {"workflow": INTERACTIVE_WORKFLOW_LABEL}}, 0),
])
async def test_plain_submit_passes_priority_to_submit_job_ex(monkeypatch, body_extra, expected):
    submit = AsyncMock(return_value=SubmitOutcome(7, False))
    monkeypatch.setattr(proxy, "submit_job_ex", submit)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    result = await proxy.jobs_submit(
        proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="hi")], **body_extra),
        Response(), capability="chat:fast", ctx=_ctx())
    assert result.job_id == 7
    assert submit.await_args.kwargs["priority"] == expected
    assert "priority" not in (submit.await_args.kwargs.get("extra") or {})


async def test_create_run_stores_priority_outside_request_and_hash():
    pid = await _project()
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=CAP, prompt="p", task_id="t",
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], priority=PRIORITY_INTERACTIVE)
    other, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=CAP, prompt="p", task_id="t2",
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    async with get_session() as s:
        row, default_row = await s.get(DeepJobRow, rid), await s.get(DeepJobRow, other)
    assert (row.priority, default_row.priority) == (0, 1)
    assert "priority" not in row.request


# --- poll ----------------------------------------------------------------------------------

async def test_poll_shows_priority():
    pid = await _project()
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=CAP, prompt="p", task_id="t",
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], priority=PRIORITY_INTERACTIVE)
    view = await get_job(rid, pid)
    assert view.priority == 0
    assert proxy._job_response(view).priority == "interactive"
    async with get_session() as s:
        row = await s.get(DeepJobRow, rid)
        row.priority = PRIORITY_BATCH
    assert proxy._job_response(await get_job(rid, pid)).priority == "batch"


# --- claim order ---------------------------------------------------------------------------

async def test_claim_orders_interactive_before_an_older_batch_job(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    pid = await _project()
    old_batch = await _pending(pid, CAP, "batch-old")
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=CAP, prompt="chat", task_id="chat",
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], priority=PRIORITY_INTERACTIVE)
    newer_batch = await _pending(pid, CAP, "batch-new")
    assert await worker.claim_agent_runs(CAP, 1) == [rid]
    assert await worker.claim_agent_runs(CAP, 5) == [old_batch, newer_batch]   # FIFO within batch


async def test_interactive_only_claim_skips_batch_jobs(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    pid = await _project()
    batch = await _pending(pid, CAP, "batch")
    assert await worker.claim_agent_runs(CAP, 3, interactive_only=True) == []
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=CAP, prompt="chat", task_id="chat",
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], priority=PRIORITY_INTERACTIVE)
    assert await worker.claim_agent_runs(CAP, 3, interactive_only=True) == [rid]
    async with get_session() as s:
        statuses = (await s.execute(select(DeepJobRow.id, DeepJobRow.status)
                                    .where(DeepJobRow.id.in_([batch, rid])))).all()
    assert dict(statuses) == {batch: "pending", rid: "running"}


def test_plain_dispatcher_claim_orders_by_priority_then_age():
    assert "ORDER BY priority, created_at" in inspect.getsource(job_queue._claim_batch)


# --- reserved slots ------------------------------------------------------------------------

@pytest.mark.parametrize("reserve, concurrency, expected", [
    (1, 1, 0),      # a one-slot lane never reserves itself out of batch work
    (1, 2, 1),
    (1, 7, 1),
    (3, 7, 3),
    (8, 4, 3),      # capped at concurrency - 1
    (0, 7, 0),
    (1, 0, 0),
])
def test_effective_interactive_reserve(reserve, concurrency, expected):
    assert worker.effective_interactive_reserve(reserve, concurrency) == expected


async def test_worker_loop_takes_only_interactive_runs_in_the_reserved_slot():
    stop, wake, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls: list[tuple[int, bool]] = []

    async def claim(cap, free, *, interactive_only=False):
        calls.append((free, interactive_only))
        return [len(calls)] if free > 0 else []

    async def guarded(run_id, cfg):
        await release.wait()

    with ExitStack() as stack:
        for name, repl in (
                ("worker_capabilities", lambda: (CAP,)),
                ("worker_image_problem", lambda **_: None),
                ("register_real_adapters", lambda caps: None),
                ("exec_config_from_settings", lambda: SimpleNamespace(workspace_root="/w")),
                ("concurrency_for", lambda cap: 2),
                ("get_settings", lambda: SimpleNamespace(AGENT_ACCOUNT_ID="a",
                                                         AGENT_INTERACTIVE_RESERVE=1)),
                ("claim_agent_runs", claim), ("_guarded", guarded),
                ("fail_stale_agent_runs", AsyncMock(return_value=0)),
                ("expire_overdue_waiting", AsyncMock(return_value=0)),
                ("fail_unclaimed_runs", AsyncMock(return_value=0))):
            stack.enter_context(patch.object(worker, name, repl))
        task = asyncio.create_task(worker.agent_worker_loop(stop, wake))
        await asyncio.sleep(0.2)
        wake.set()                      # next tick: one slot in flight, one free = the reserve
        await asyncio.sleep(0.2)
        stop.set()
        release.set()
        wake.set()
        await asyncio.wait_for(task, timeout=5)
    assert calls[0] == (2, False)       # both slots free: batch work allowed
    assert calls[1] == (1, True)        # free slots <= reserve: interactive only


# --- schema --------------------------------------------------------------------------------

def test_migration_023_adds_priority_and_init_sql_mirrors_it():
    assert ("deep_jobs", "priority") in _migration_columns()
    assert ("deep_jobs", "priority") in _init_columns()
