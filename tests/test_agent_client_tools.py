"""Durable client tool handoff: ownership, schema, replay, and terminal state."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aibroker.agent.client_tools import (
    ClientToolConflict,
    publish_call,
    submit_result,
    take_result,
)
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.routes import proxy

TOOLS = [{"type": "function", "function": {"name": "lookup",
          "parameters": {"type": "object", "properties": {"id": {"type": "integer"}},
                         "required": ["id"]}}}]


async def _job() -> int:
    key = generate_project_key()
    async with get_session() as s:
        s.add(ProjectRow(id=91, name="tool-test", project_key_hash=hash_project_key(key),
                         project_key_prefix=key[:12], allowed_scopes=["llm:agent"],
                         is_active=True, notes=""))
        s.add(ProjectRow(id=92, name="other-tool-test", project_key_hash=hash_project_key(key),
                         project_key_prefix=key[:12], allowed_scopes=["llm:agent"],
                         is_active=True, notes=""))
        await s.flush()
        row = DeepJobRow(project_id=91, capability="agent:codex", status="running",
                         request={"model": "gpt-6.1-sol", "account_id": "owner",
                                  "client_job": {"tools": TOOLS, "tool_choice": "auto"}})
        s.add(row)
        await s.flush()
        s.add(AgentRunRow(run_id=row.id, project_id=91, capability="agent:codex",
                          task_id="tool-fixture", account_id="owner",
                          model="gpt-6.1-sol"))
        return row.id


async def test_client_tool_result_is_owned_and_idempotent(monkeypatch):
    job_id = await _job()
    with pytest.raises(ClientToolConflict, match="schema"):
        await publish_call(job_id, "bad", "lookup", {"id": "string"})
    await publish_call(job_id, "c1", "lookup", {"id": 3})
    with pytest.raises(LookupError):
        await submit_result(job_id, 92, "c1", True, "ok")
    with pytest.raises(ClientToolConflict, match="call id"):
        await submit_result(job_id, 91, "other", True, "ok")
    with pytest.raises(ValueError, match="limit"):
        await submit_result(job_id, 91, "c1", True, "x" * 65537)
    assert await take_result(job_id, "c1") is None
    assert await submit_result(job_id, 91, "c1", True, "ok") is True
    assert await submit_result(job_id, 91, "c1", True, "ok") is False
    with pytest.raises(ClientToolConflict, match="different"):
        await submit_result(job_id, 91, "c1", True, "changed")
    assert await take_result(job_id, "c1") == (True, "ok")
    assert await submit_result(job_id, 91, "c1", True, "ok") is False
    with pytest.raises(ClientToolConflict, match="already pending"):
        await publish_call(job_id, "c1", "lookup", {"id": 3})
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    ctx = SimpleNamespace(project=SimpleNamespace(id=92), has_scope=lambda _: True)
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_tool_result(job_id, proxy.ClientToolResultIn(
            call_id="c1", success=True, content="ok"), ctx=ctx)
    assert exc.value.status_code == 404


async def test_pending_job_exposes_only_declared_call_and_deadline_blocks_result(monkeypatch):
    job_id = await _job()
    await publish_call(job_id, "c1", "lookup", {"id": 3})
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        response = proxy._job_response(row)
        assert response.status == "pending" and response.finish_reason == "tool_calls"
        assert response.tool_calls == [{"id": "c1", "type": "function",
                                        "function": {"name": "lookup", "arguments": '{"id":3}'}}]
        meta = dict(row.result_meta)
        meta["client_tool"] = {**meta["client_tool"], "deadline": "2000-01-01T00:00:00"}
        row.result_meta = meta
    with pytest.raises(ClientToolConflict, match="expired"):
        await submit_result(job_id, 91, "c1", True, "too late")
    with pytest.raises(ClientToolConflict, match="expired"):
        await take_result(job_id, "c1")
