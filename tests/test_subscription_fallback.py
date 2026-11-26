"""Per-project subscription fallback: free keys only, then the same job goes to codex-sub."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.services import job_queue, subscription_fallback

MODEL = "codex-sub/gpt-6.1-sol"
_REQ = {"messages": [{"role": "system", "content": "You triage chats."},
                     {"role": "user", "content": "chat 1: hi"}],
        "model": None, "max_tokens": 1500, "temperature": 0.7,
        "response_format": {"type": "json_object"}, "workflow": "triage"}


@pytest.fixture
def fallback_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "SUBSCRIPTION_FALLBACK", '{"990901": "%s"}' % MODEL)
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    monkeypatch.setattr(subscription_fallback, "subscription_model_verified", lambda m: True)
    monkeypatch.setattr(subscription_fallback, "project_allowed", lambda pid: True)
    monkeypatch.setattr(subscription_fallback, "policy_violation", lambda *a: None)
    monkeypatch.setattr(subscription_fallback, "wake_worker", AsyncMock())


def test_applies_only_to_listed_projects_and_chat_lanes(fallback_on, monkeypatch):
    assert subscription_fallback.applies(990901, "chat:smart")
    assert subscription_fallback.applies(990901, "structured")
    assert not subscription_fallback.applies(990901, "chat:deep")
    assert not subscription_fallback.applies(990901, "image")
    assert not subscription_fallback.applies(2, "chat:smart")
    monkeypatch.setattr(get_settings(), "SUBSCRIPTION_FALLBACK", "not json")
    assert not subscription_fallback.applies(990901, "chat:smart")


def test_off_by_default():
    assert get_settings().SUBSCRIPTION_FALLBACK == "{}"
    assert not subscription_fallback.applies(2, "chat:smart")


async def _project(pid: int, scopes: list[str]) -> ProjectRow:
    async with get_session() as s:
        row = ProjectRow(id=pid, name=f"fb-{pid}", project_key_hash="h", project_key_prefix="pk_x",
                         allowed_scopes=scopes)
        s.add(row)
    return row


async def _running_job(pid: int, job_id: int) -> DeepJobRow:
    started = datetime(2026, 11, 26, 13, 1)
    async with get_session() as s:
        s.add(DeepJobRow(id=job_id, project_id=pid, capability="chat:smart", status="running",
                         retry_count=0, request=dict(_REQ), started_at=started))
    return DeepJobRow(id=job_id, project_id=pid, capability="chat:smart", status="running",
                      retry_count=0, request=dict(_REQ), started_at=started)


async def test_hand_off_turns_the_job_into_a_codex_run_in_place(fallback_on):
    project = await _project(990901, ["llm:chat", "llm:agent"])
    row = await _running_job(990901, 990901)
    assert await subscription_fallback.hand_off(row, project, dict(_REQ), "no free provider")
    async with get_session() as s:
        job = await s.get(DeepJobRow, 990901)
        run = await s.get(AgentRunRow, 990901)
    assert (job.capability, job.status, job.started_at, job.retry_count) == (
        "agent:codex", "pending", None, 0)
    assert job.request["model"] == "gpt-6.1-sol" and "chat 1: hi" in job.request["prompt"]
    client = job.request["client_job"]
    assert client["model"] == MODEL and client["capability"] == "chat:smart"
    assert client["response_format"] == {"type": "json_object"}
    assert client["fallback_reason"] == "no free provider"
    assert run is not None and run.model == "gpt-6.1-sol" and run.account_id == "owner"
    subscription_fallback.wake_worker.assert_awaited_once_with("agent:codex", 990901)


async def test_no_hand_off_without_agent_scope(fallback_on):
    project = await _project(990901, ["llm:chat"])
    row = await _running_job(990901, 990902)
    assert not await subscription_fallback.hand_off(row, project, dict(_REQ), "no free provider")
    async with get_session() as s:
        assert (await s.get(DeepJobRow, 990902)).capability == "chat:smart"


async def test_no_hand_off_for_client_tools(fallback_on):
    project = await _project(990901, ["llm:chat", "llm:agent"])
    row = await _running_job(990901, 990903)
    req = {**_REQ, "tools": [{"type": "function", "function": {"name": "x"}}]}
    assert not await subscription_fallback.hand_off(row, project, req, "no free provider")


def _row(pid: int, key_tier: str | None = None) -> DeepJobRow:
    req = dict(_REQ)
    if key_tier:
        req["key_tier"] = key_tier
    return DeepJobRow(id=1, project_id=pid, capability="chat:smart", status="running",
                      retry_count=0, request=req)


@pytest.mark.parametrize(("key_tier", "expect_tier", "expect_handoff"), [
    (None, "free", True),       # fallback project: free keys only, then the subscription
    ("paid", "paid", False),    # an explicit paid request keeps its own path
])
async def test_dispatcher_walks_free_then_hands_off(fallback_on, key_tier, expect_tier,
                                                    expect_handoff):
    await _project(990901, ["llm:chat", "llm:agent"])
    seen = []

    async def fake_run_chat(**kw):
        seen.append(kw["key_tier"])

    with patch.object(job_queue, "run_chat", fake_run_chat), \
         patch.object(subscription_fallback, "hand_off", AsyncMock(return_value=True)) as hand, \
         patch.object(job_queue, "_requeue_or_fail", AsyncMock()) as requeue:
        await job_queue._execute(_row(990901, key_tier))
    assert seen == [expect_tier]
    assert hand.await_count == (1 if expect_handoff else 0)
    assert requeue.await_count == (0 if expect_handoff else 1)
