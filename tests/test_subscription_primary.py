"""Subscription-first chat lanes: the job runs on codex-sub first, back to the API walk on failure."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.agent.events import RunCompleted, RunFailed, RunTimedOut
from aibroker.agent.runs import append_event
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.services import job_queue, subscription_primary

MODEL = "codex-sub/gpt-6.1-sol"
PID = 990901
_REQ = {"messages": [{"role": "system", "content": "You triage chats."},
                     {"role": "user", "content": "chat 1: hi"}],
        "model": None, "max_tokens": 1500, "temperature": 0.7,
        "response_format": {"type": "json_object"}, "workflow": "triage"}


@pytest.fixture
def primary_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "SUBSCRIPTION_PRIMARY", json.dumps(
        {str(PID): {"model": MODEL, "capabilities": ["chat:smart"]}}))
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    monkeypatch.setattr(subscription_primary, "subscription_model_verified", lambda m: True)
    monkeypatch.setattr(subscription_primary, "project_allowed", lambda pid: True)
    monkeypatch.setattr(subscription_primary, "policy_violation", lambda *a: None)
    monkeypatch.setattr(subscription_primary, "wake_worker", AsyncMock())


def test_primary_model_matches_only_listed_project_and_lanes(primary_on, monkeypatch):
    assert subscription_primary.primary_model(PID, "chat:smart") == MODEL
    assert subscription_primary.primary_model(PID, "chat:fast") is None     # not listed
    assert subscription_primary.primary_model(PID, "chat:deep") is None
    assert subscription_primary.primary_model(2, "chat:smart") is None
    assert not subscription_primary.wants_subscription(PID, "chat:smart",
                                                       {"subscription_tried": {"outcome": "x"}})
    assert not subscription_primary.wants_subscription(PID, "chat:smart", {"key_tier": "paid"})
    assert subscription_primary.wants_subscription(PID, "chat:smart", dict(_REQ))
    monkeypatch.setattr(get_settings(), "SUBSCRIPTION_PRIMARY", "not json")
    assert subscription_primary.primary_model(PID, "chat:smart") is None


def test_off_by_default():
    assert get_settings().SUBSCRIPTION_PRIMARY == "{}"
    assert subscription_primary.primary_model(2, "chat:smart") is None


async def _project(scopes: list[str]) -> ProjectRow:
    async with get_session() as s:
        row = ProjectRow(id=PID, name=f"sp-{PID}", project_key_hash="h", project_key_prefix="pk_x",
                         allowed_scopes=scopes)
        s.add(row)
    return row


async def _running_job(job_id: int) -> DeepJobRow:
    started = datetime(2026, 11, 26, 13, 1)
    async with get_session() as s:
        s.add(DeepJobRow(id=job_id, project_id=PID, capability="chat:smart", status="running",
                         retry_count=0, request=dict(_REQ), started_at=started))
    return DeepJobRow(id=job_id, project_id=PID, capability="chat:smart", status="running",
                      retry_count=0, request=dict(_REQ), started_at=started)


async def test_hand_off_turns_the_job_into_a_codex_run_in_place(primary_on):
    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990901)
    assert await subscription_primary.hand_off(row, project, dict(_REQ))
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990901)
        run = await s.get(AgentRunRow, 990901)
    assert (job.capability, job.status, job.started_at) == ("agent:codex", "pending", None)
    assert job.request["model"] == "gpt-6.1-sol" and "chat 1: hi" in job.request["prompt"]
    assert job.request["deadline_s"] == subscription_primary.PRIMARY_DEADLINE_S
    client = job.request["client_job"]
    assert client["model"] == MODEL and client["subscription_first"] is True
    assert client["no_shell"] is True                 # third-party chat text: no shell
    assert client["fallback_capability"] == "chat:smart" and client["fallback_request"] == _REQ
    assert run is not None and run.model == "gpt-6.1-sol" and run.account_id == "owner"
    subscription_primary.wake_worker.assert_awaited_once_with("agent:codex", 990901)


async def test_no_hand_off_without_agent_scope_or_with_tools(primary_on):
    project = await _project(["llm:chat"])
    row = await _running_job(990902)
    assert not await subscription_primary.hand_off(row, project, dict(_REQ))
    project.allowed_scopes = ["llm:chat", "llm:agent"]
    assert not await subscription_primary.hand_off(
        row, project, {**_REQ, "tools": [{"type": "function", "function": {"name": "x"}}]})
    async with get_session() as s:
        assert (await s.get(DeepJobRow, 990902)).capability == "chat:smart"


@pytest.mark.parametrize("event", [
    RunFailed(error_class="quota_exhausted", message="limit reached"),
    RunTimedOut(limit="deadline"),
])
async def test_a_failed_subscription_run_goes_back_to_the_api_walk(primary_on, event):
    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990903)
    assert await subscription_primary.hand_off(row, project, dict(_REQ))
    await append_event(990903, event)
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990903)
    assert (job.capability, job.status, job.error_message) == ("chat:smart", "pending", None)
    assert job.request["messages"] == _REQ["messages"]
    assert job.request["subscription_tried"]["model"] == MODEL
    assert not subscription_primary.wants_subscription(PID, "chat:smart", job.request)


async def test_a_completed_subscription_run_finishes_the_job(primary_on):
    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990904)
    assert await subscription_primary.hand_off(row, project, dict(_REQ))
    await append_event(990904, RunCompleted(final_text='{"action": "reply_now"}'))
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990904)
    assert (job.capability, job.status) == ("agent:codex", "done")
    assert job.result_text == '{"action": "reply_now"}'


@pytest.mark.parametrize(("req", "expect_handoff"), [
    (dict(_REQ), True),
    ({**_REQ, "subscription_tried": {"outcome": "failed"}}, False),   # second pass: API walk
])
async def test_dispatcher_tries_the_subscription_first(primary_on, req, expect_handoff):
    await _project(["llm:chat", "llm:agent"])
    row = DeepJobRow(id=1, project_id=PID, capability="chat:smart", status="running",
                     retry_count=0, request=req)
    walked = []

    async def fake_run_chat(**kw):
        walked.append(kw["key_tier"])

    with patch.object(job_queue, "run_chat", fake_run_chat), \
         patch.object(subscription_primary, "hand_off", AsyncMock(return_value=True)) as hand, \
         patch.object(job_queue, "_requeue_or_fail", AsyncMock()):
        await job_queue._execute(row)
    assert hand.await_count == (1 if expect_handoff else 0)
    assert walked == ([] if expect_handoff else ["auto"])     # the normal walk, order unchanged


async def test_quota_exhaustion_fails_over_instead_of_waiting(monkeypatch):
    from types import SimpleNamespace

    from aibroker.agent import executor

    finished = []

    async def fake_finish(run_id, terminal, totals, mode=None, lease=None):
        finished.append((run_id, terminal))
        return "failed"

    parked = AsyncMock()
    monkeypatch.setattr(executor, "_finish", fake_finish)
    monkeypatch.setattr(executor, "park_for_quota", parked)
    run = SimpleNamespace(run_id=7, quota_waits=0)
    job = SimpleNamespace(created_at=datetime(2026, 11, 26, 13, 1),
                          request={"deadline_s": 300, "client_job": {"subscription_first": True}})
    out = await executor._wait_for_quota(
        run, job, RunFailed(error_class="quota_exhausted", message="limit", reset_at=None),
        {}, "subscription", SimpleNamespace(quota_backoff_base_s=60, quota_backoff_cap_s=3600))
    assert out == "failed" and finished[0][1].error_class == "quota_exhausted"
    parked.assert_not_awaited()


async def test_a_cancelled_job_is_not_handed_off(primary_on):
    from sqlalchemy import update

    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990905)
    async with get_session() as s:
        await s.execute(update(DeepJobRow).where(DeepJobRow.id == 990905)
                        .values(cancel_requested=True))
    assert not await subscription_primary.hand_off(row, project, dict(_REQ))
    async with get_session() as s:
        assert (await s.get(DeepJobRow, 990905)).capability == "chat:smart"


def test_a_pinned_api_model_keeps_its_api_path(primary_on):
    assert not subscription_primary.wants_subscription(
        PID, "chat:smart", {**_REQ, "model": "gemini/gemini-3.6-flash"})


async def test_overdue_pending_runs_go_back_to_the_api_walk(primary_on):
    from datetime import timedelta

    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990906)
    assert await subscription_primary.hand_off(row, project, dict(_REQ))
    async with get_session() as s:
        created = (await s.get(DeepJobRow, 990906)).created_at
    later = created + timedelta(seconds=subscription_primary.PRIMARY_DEADLINE_S + 5)
    assert await subscription_primary.expire_overdue(now=later) == 1
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990906)
    assert (job.capability, job.status) == ("chat:smart", "pending")
    assert job.request["subscription_tried"]["outcome"] == "timed_out"
    assert await subscription_primary.expire_overdue(now=later) == 0


async def test_a_failure_after_a_cancel_request_does_not_revert(primary_on):
    from sqlalchemy import update

    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990907)
    assert await subscription_primary.hand_off(row, project, dict(_REQ))
    async with get_session() as s:
        await s.execute(update(AgentRunRow).where(AgentRunRow.run_id == 990907)
                        .values(cancel_requested=True))
    await append_event(990907, RunFailed(error_class="cli_error", message="boom"))
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990907)
    assert job.capability == "agent:codex" and job.status == "error"


async def test_the_kept_request_is_dropped_when_the_run_completes(primary_on):
    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990908)
    assert await subscription_primary.hand_off(row, project, dict(_REQ))
    await append_event(990908, RunCompleted(final_text="{}"))
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990908)
    assert "fallback_request" not in job.request["client_job"]
    assert job.request["client_job"]["subscription_first"] is True


def test_lane_share_and_queue_timeout_defaults_and_overrides(primary_on, monkeypatch):
    assert subscription_primary.max_concurrent(PID) == subscription_primary.DEFAULT_MAX_CONCURRENT
    assert subscription_primary.queue_timeout_s(PID) == subscription_primary.DEFAULT_QUEUE_TIMEOUT_S
    monkeypatch.setattr(get_settings(), "SUBSCRIPTION_PRIMARY", json.dumps(
        {str(PID): {"model": MODEL, "capabilities": ["chat:smart"], "max_concurrent": 3,
                    "queue_timeout_s": 45}}))
    assert subscription_primary.max_concurrent(PID) == 3
    assert subscription_primary.queue_timeout_s(PID) == 45
    monkeypatch.setattr(get_settings(), "SUBSCRIPTION_PRIMARY", json.dumps(
        {str(PID): {"model": MODEL, "capabilities": ["chat:smart"], "max_concurrent": 0}}))
    assert subscription_primary.max_concurrent(PID) == subscription_primary.DEFAULT_MAX_CONCURRENT


async def test_a_full_lane_share_sends_the_job_straight_to_the_api_walk(primary_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "SUBSCRIPTION_PRIMARY", json.dumps(
        {str(PID): {"model": MODEL, "capabilities": ["chat:smart"], "max_concurrent": 1}}))
    project = await _project(["llm:chat", "llm:agent"])
    first = await _running_job(990911)
    assert await subscription_primary.hand_off(first, project, dict(_REQ))
    second = await _running_job(990912)
    assert not await subscription_primary.hand_off(second, project, dict(_REQ))
    async with get_session() as s:
        assert (await s.get(DeepJobRow, 990912)).capability == "chat:smart"


async def test_a_run_waiting_too_long_for_a_slot_goes_back_to_the_api_walk(primary_on):
    from datetime import timedelta

    project = await _project(["llm:chat", "llm:agent"])
    row = await _running_job(990913)
    assert await subscription_primary.hand_off(row, project, dict(_REQ))
    async with get_session() as s:
        handed = (await s.get(DeepJobRow, 990913)).run_after
    soon = handed + timedelta(seconds=subscription_primary.DEFAULT_QUEUE_TIMEOUT_S - 5)
    assert await subscription_primary.expire_overdue(now=soon) == 0
    late = handed + timedelta(seconds=subscription_primary.DEFAULT_QUEUE_TIMEOUT_S + 1)
    assert await subscription_primary.expire_overdue(now=late) == 1
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990913)
    assert (job.capability, job.status) == ("chat:smart", "pending")


async def test_the_sweep_is_free_when_nothing_is_configured():
    assert get_settings().SUBSCRIPTION_PRIMARY == "{}"
    assert not subscription_primary.enabled()
    assert await subscription_primary.expire_overdue() == 0
