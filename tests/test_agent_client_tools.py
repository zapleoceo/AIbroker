"""Durable client tool handoff: ownership, schema, replay, and terminal state."""
from __future__ import annotations

import asyncio
import json
import tracemalloc
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_agent_codex_app_server import TOOL, _Adapter, _Process, _Writer

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.client_tools import (
    ClientToolConflict,
    publish_call,
    submit_result,
    take_result,
)
from aibroker.agent.codex_app_server import run_tool_turn
from aibroker.agent.executor import _stream, exec_config_from_settings
from aibroker.agent.lock import Lease, StaleLease
from aibroker.agent.runs import create_run
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import Settings, get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow, DeepJobRow, ProjectRow
from aibroker.providers import subscriptions
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
        await submit_result(job_id, 91, "c1", True, "x" * (1024 * 1024 + 1))
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


def test_tool_canary_is_exact_project_and_model(monkeypatch):
    settings = SimpleNamespace(AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS="12",
                               AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_MODELS='["codex-sub/gpt-6.1-sol"]')
    monkeypatch.setattr(subscriptions, "get_settings", lambda: settings)
    monkeypatch.setattr(subscriptions, "subscription_model_verified", lambda _: True)
    assert subscriptions.subscription_tool_model_canary("codex-sub/gpt-6.1-sol", 12)
    assert not subscriptions.subscription_tool_model_canary("codex-sub/gpt-6.1-sol", 11)
    assert not subscriptions.subscription_tool_model_canary("codex-sub/gpt-6-luna", 12)
    settings.AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS = "12,12"
    assert not subscriptions.subscription_tool_model_canary("codex-sub/gpt-6.1-sol", 12)


async def _expire(job_id: int) -> None:
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        meta = dict(row.result_meta)
        meta["client_tool"] = {**meta["client_tool"], "deadline": "2000-01-01T00:00:00"}
        row.result_meta = meta


async def test_take_result_returns_ready_result_after_deadline():
    job_id = await _job()
    await publish_call(job_id, "c1", "lookup", {"id": 3})
    assert await submit_result(job_id, 91, "c1", True, "ok") is True
    await _expire(job_id)       # the worker's ~1s poll lands after the deadline
    assert await take_result(job_id, "c1") == (True, "ok")


async def test_poll_hides_expired_pending_call():
    job_id = await _job()
    await publish_call(job_id, "c1", "lookup", {"id": 3})
    await _expire(job_id)
    async with get_session() as s:
        response = proxy._job_response(await s.get(DeepJobRow, job_id))
    assert response.status == "pending"
    assert response.tool_calls is None and response.finish_reason is None
    assert response.poll_after_s is not None


class _Idle:
    """Adapter that never emits: the run is parked in a tool wait when its lease is lost."""

    async def start(self, spec):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    async def cancel(self):
        return None


async def test_lease_loss_withdraws_pending_call():
    job_id = await _job()
    await publish_call(job_id, "c1", "lookup", {"id": 3})
    lease = Lease("owner", "s", job_id, 1)
    lease.lost.set()
    spec = AgentRunSpec(run_id=job_id, capability="agent:codex", prompt="p",
                        workspace=".", account_id="owner")
    with pytest.raises(StaleLease):
        await _stream(job_id, _Idle(), spec, {}, 60.0, 60.0, exec_config_from_settings(), lease)
    async with get_session() as s:
        assert proxy._job_response(await s.get(DeepJobRow, job_id)).tool_calls is None
    with pytest.raises(ClientToolConflict):
        await submit_result(job_id, 91, "c1", True, "ok")


async def test_cancel_during_tool_wait_yields_cancelled_outcome(monkeypatch, tmp_path):
    job_id = await _job()
    home = tmp_path / "codex-home"
    home.mkdir()
    spec = AgentRunSpec(run_id=job_id, capability="agent:codex", prompt="Find id 7",
                        workspace=str(tmp_path), account_id="owner", model="gpt-6.1-sol",
                        client_tools=[TOOL], tool_choice="required")

    async def spawn(*args, **kwargs):
        return _Process()

    async def publish_then_cancel(run_id, call_id, name, arguments, fences):
        await publish_call(run_id, call_id, name, arguments, fences)
        async with get_session() as s:
            (await s.get(AgentRunRow, run_id)).cancel_requested = True

    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.publish_call", publish_then_cancel)
    monkeypatch.setattr("aibroker.agent.codex_app_server.permission_overrides",
                        lambda home: [])   # the POSIX CODEX_HOME check is not under test

    class Adapter(_Adapter):
        def start(self, run):
            return run_tool_turn(self, run)

    cfg = replace(exec_config_from_settings(), cancel_poll_s=60.0)   # only the tool wait can notice
    terminal, _ = await _stream(job_id, Adapter(home), spec, {}, 60.0, 60.0, cfg)
    assert ev.OUTCOME_BY_TYPE[terminal.type] == "cancelled"


async def test_lease_loss_keeps_accepted_ready_result():
    job_id = await _job()
    await publish_call(job_id, "c1", "lookup", {"id": 3})
    assert await submit_result(job_id, 91, "c1", True, "ok") is True
    lease = Lease("owner", "s", job_id, 1)
    lease.lost.set()
    spec = AgentRunSpec(run_id=job_id, capability="agent:codex", prompt="p",
                        workspace=".", account_id="owner")
    with pytest.raises(StaleLease):
        await _stream(job_id, _Idle(), spec, {}, 60.0, 60.0, exec_config_from_settings(), lease)
    assert await submit_result(job_id, 91, "c1", True, "ok") is False   # idempotent retry
    assert await take_result(job_id, "c1") == (True, "ok")


async def test_lease_loss_still_raises_when_withdrawing_the_call_fails(monkeypatch, caplog):
    job_id = await _job()
    await publish_call(job_id, "c1", "lookup", {"id": 3})

    async def broken(run_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("aibroker.agent.executor.clear_pending_call", broken)
    lease = Lease("owner", "s", job_id, 1)
    lease.lost.set()
    spec = AgentRunSpec(run_id=job_id, capability="agent:codex", prompt="p",
                        workspace=".", account_id="owner")
    with caplog.at_level("ERROR"), pytest.raises(StaleLease):
        await _stream(job_id, _Idle(), spec, {}, 60.0, 60.0, exec_config_from_settings(), lease)
    assert any("could not withdraw the pending client tool" in r.message and r.exc_info
               for r in caplog.records)


def test_client_tool_limit_defaults_are_the_documented_ones():
    fields = Settings.model_fields
    assert fields["AGENT_CLIENT_TOOL_MAX_CALLS"].default == 64
    assert fields["AGENT_CLIENT_TOOL_MAX_BYTES"].default == 1024 * 1024
    assert fields["AGENT_CLIENT_TOOL_WAIT_S"].default == 900
    assert fields["AGENT_SUBSCRIPTION_MAX_WALL_S"].default == 14400
    assert fields["AGENT_SUBSCRIPTION_IDLE_S"].default == 300


async def test_call_count_argument_size_and_wait_follow_settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_CLIENT_TOOL_MAX_CALLS", 2)
    monkeypatch.setattr(get_settings(), "AGENT_CLIENT_TOOL_MAX_BYTES", 2048)
    monkeypatch.setattr(get_settings(), "AGENT_CLIENT_TOOL_WAIT_S", 7)
    job_id = await _job()
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        row.request = {**row.request, "client_job": {"tools": tools, "tool_choice": "auto"}}
    with pytest.raises(ClientToolConflict, match="arguments exceed"):
        await publish_call(job_id, "big", "lookup", {"pad": "x" * 2048})
    for i in range(2):
        await publish_call(job_id, f"c{i}", "lookup", {})
        async with get_session() as s:
            deadline = (await s.get(DeepJobRow, job_id)).result_meta["client_tool"]["deadline"]
        assert 0 < (datetime.fromisoformat(deadline) - datetime.now(UTC).replace(tzinfo=None)
                    ).total_seconds() <= 7
        assert await submit_result(job_id, 91, f"c{i}", True, "ok") is True
        assert await take_result(job_id, f"c{i}") == (True, "ok")
    with pytest.raises(ClientToolConflict, match="limit reached"):
        await publish_call(job_id, "c2", "lookup", {})
    with pytest.raises(ValueError, match="limit"):
        await submit_result(job_id, 91, "c1", True, "y" * 2049)


async def test_one_mib_tool_results_and_prompt_limit_stay_bounded_in_memory():
    """Each round trip moves a 1 MiB result through the row; none may accumulate in result_meta,
    and a prompt at the default limit is stored once, not multiplied per round trip."""
    job_id = await _job()
    payload = "r" * (1024 * 1024)
    tracemalloc.start()
    try:
        for i in range(5):
            await publish_call(job_id, f"m{i}", "lookup", {"id": i})
            assert await submit_result(job_id, 91, f"m{i}", True, payload) is True
            assert await take_result(job_id, f"m{i}") == (True, payload)
        await create_run(
            project=SimpleNamespace(id=91), capability="agent:claude",
            prompt="p" * get_settings().AGENT_SUBSCRIPTION_PROMPT_MAX_CHARS, task_id="mem",
            account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
            idle_timeout_s=30, allowed_tools=[])
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    async with get_session() as s:
        meta = (await s.get(DeepJobRow, job_id)).result_meta
    assert "client_tool" not in meta and len(json.dumps(meta)) < 2048
    assert peak < 96 * 1024 * 1024


class _TimedWriter(_Writer):
    """After turn/start: the turn reply, then `feed(reader)` runs in the background."""

    def __init__(self, reader, feed):
        super().__init__(reader)
        self.feed = feed

    def write(self, data):
        message = json.loads(data)
        if message.get("method") == "turn/start":
            self.sent.append(message)
            self.reader.put({"id": 3, "result": {"turn": {"id": "turn-1"}}})
            if self.feed:
                self._task = asyncio.get_running_loop().create_task(self.feed(self.reader))
        else:
            super().write(data)


async def _app_server_stream(monkeypatch, tmp_path, feed, idle_s):
    job_id = await _job()
    home = tmp_path / "codex-home"
    home.mkdir()
    spec = AgentRunSpec(run_id=job_id, capability="agent:codex", prompt="think",
                        workspace=str(tmp_path), account_id="owner", model="gpt-6.1-sol",
                        client_tools=[TOOL], tool_choice=None)
    process = _Process()
    process.stdin = _TimedWriter(process.stdout, feed)

    async def spawn(*_a, **_k):
        return process

    monkeypatch.setattr("aibroker.agent.codex_app_server.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr("aibroker.agent.codex_app_server.permission_overrides", lambda _h: [])

    class Adapter(_Adapter):
        def start(self, run):
            return run_tool_turn(self, run)

    terminal, _ = await _stream(job_id, Adapter(home), spec, {}, 60.0, idle_s,
                                exec_config_from_settings())
    async with get_session() as s:
        rows = (await s.execute(select(AgentRunEventRow.type)
                                .where(AgentRunEventRow.run_id == job_id))).scalars().all()
    return terminal, rows


async def test_item_notifications_keep_a_long_reasoning_turn_alive_past_idle(monkeypatch, tmp_path):
    async def feed(reader):
        for _ in range(6):                       # 1.2 s of reasoning/command items, idle is 0.5 s
            await asyncio.sleep(0.2)
            reader.put({"method": "item/started", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "item": {"type": "reasoning"}}})
            reader.put({"method": "item/completed", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "item": {"type": "reasoning"}}})
        reader.put({"method": "turn/completed", "params": {
            "threadId": "thread-1", "turn": {"id": "turn-1", "status": "completed"}}})

    terminal, rows = await _app_server_stream(monkeypatch, tmp_path, feed, 0.5)
    assert terminal.type == "run.completed"
    assert "progress" not in rows


async def test_notifications_of_another_turn_do_not_keep_the_run_alive(monkeypatch, tmp_path):
    async def feed(reader):
        for _ in range(6):
            await asyncio.sleep(0.2)
            reader.put({"method": "item/started", "params": {
                "threadId": "thread-1", "turnId": "other", "item": {"type": "reasoning"}}})

    terminal, _ = await _app_server_stream(monkeypatch, tmp_path, feed, 0.5)
    assert terminal.type == "run.timed_out" and terminal.limit == "idle"


async def test_a_truly_silent_app_server_is_ended_by_idle(monkeypatch, tmp_path):
    terminal, _ = await _app_server_stream(monkeypatch, tmp_path, None, 0.5)
    assert terminal.type == "run.timed_out" and terminal.limit == "idle"
