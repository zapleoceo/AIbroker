"""Durable client tool handoff: ownership, schema, replay, and terminal state."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from test_agent_codex_app_server import TOOL, _Adapter, _Process

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
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
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


@pytest.mark.parametrize("wait_s", [90, 300])
async def test_tool_call_deadline_follows_agent_tool_wait_s(monkeypatch, wait_s):
    from datetime import UTC, datetime

    from aibroker.config import get_settings
    monkeypatch.setattr(get_settings(), "AGENT_TOOL_WAIT_S", wait_s)
    job_id = await _job()
    before = datetime.now(UTC).replace(tzinfo=None)
    await publish_call(job_id, "c1", "lookup", {"id": 3})
    async with get_session() as s:
        deadline = datetime.fromisoformat(
            (await s.get(DeepJobRow, job_id)).result_meta["client_tool"]["deadline"])
    assert wait_s - 5 <= (deadline - before).total_seconds() <= wait_s + 5


LANDING = [{"type": "function", "function": {"name": "final_result", "parameters": {
    "type": "object", "additionalProperties": False, "required": ["css", "body", "concept_note"],
    "properties": {"css": {"type": "string", "maxLength": 40000},
                   "body": {"type": "string", "maxLength": 60000},
                   "concept_note": {"type": "string", "maxLength": 400}}}}}]


def test_schema_violation_names_the_rule_but_never_a_value():
    from aibroker.agent.client_tools import schema_violation
    secret = "SYNTHETIC-SECRET-VALUE " * 30                          # 690 chars, over 400
    args = {"css": "a", "body": "b", "concept_note": secret, "extra_key": "v"}
    detail = schema_violation(LANDING, "final_result", args)
    assert "concept_note: maxLength 400 (got len 690)" in detail
    assert "<root>: additionalProperties" in detail
    assert "SYNTHETIC" not in detail and "extra_key" not in detail
    assert "required" in schema_violation(LANDING, "final_result", {"css": "a", "body": "b"})
    assert "type string (got type int)" in schema_violation(
        LANDING, "final_result", {"css": 1, "body": "b", "concept_note": "c"})
    assert schema_violation(LANDING, "nope", {}) == "undeclared tool 'nope'"


async def test_publish_call_rejection_carries_the_schema_detail(monkeypatch):
    job_id = await _job()
    with pytest.raises(ClientToolConflict, match=r"outside the declared schema \[InvalidToolArguments\]: id: type integer \(got type str\)"):
        await publish_call(job_id, "c1", "lookup", {"id": "string"})


async def test_publish_call_rejection_logs_label_and_rule_without_values(caplog):
    import logging
    job_id = await _job()
    with caplog.at_level(logging.WARNING, logger="aibroker.agent.client_tools"), \
            pytest.raises(ClientToolConflict):
        await publish_call(job_id, "c1", "lookup", {"id": "SYNTHETIC-SECRET-VALUE"})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "label=InvalidToolArguments" in text and "id: type integer" in text
    assert "SYNTHETIC" not in text


def test_schema_violation_never_shows_a_model_chosen_key():
    from aibroker.agent.client_tools import schema_violation
    tools = [{"type": "function", "function": {"name": "t", "parameters": {
        "type": "object", "properties": {"m": {"type": "object",
                                               "additionalProperties": {"type": "integer"}}},
        "additionalProperties": {"type": "integer"},
        "patternProperties": {"^LEAK": {"type": "integer"}}}}}]
    args = {"LEAKED-KEY-sk-abc123": "x", "LEAK2": "y", "m": {"MODELKEY": "v"}}
    detail = schema_violation(tools, "t", args)
    assert "LEAK" not in detail and "MODELKEY" not in detail and "sk-" not in detail
    assert "*: type integer (got type str)" in detail and "m/*: type integer" in detail


def test_schema_violation_stops_after_three_errors_on_a_large_argument():
    import time

    from aibroker.agent.client_tools import schema_violation
    tools = [{"type": "function", "function": {"name": "t", "parameters": {
        "type": "object", "properties": {"a": {"type": "array", "items": {"type": "string"}}}}}}]
    started = time.monotonic()
    detail = schema_violation(tools, "t", {"a": list(range(30000))})
    assert time.monotonic() - started < 0.5
    assert detail.count("type string") == 3 and detail.startswith("a/0:")
    no_params = [{"type": "function", "function": {"name": "t"}}]
    assert schema_violation(no_params, "t", {}) == "tool has no parameters schema"
