"""/ask2many Codex runs get no command tools (AgentRunSpec.no_shell -> codex --disable ...)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.codex_cli import NO_SHELL_FEATURES, build_codex_argv
from aibroker.routes import proxy
from aibroker.services.deep_jobs import build_request


def _spec(**kw):
    return AgentRunSpec(run_id=1, capability="agent:codex", prompt="p", workspace="/w",
                        account_id="owner", model="gpt-6.1-sol", **kw)


def test_no_shell_disables_every_command_tool_and_only_then():
    plain = build_codex_argv(_spec(), "codex", "/home/agent/.codex")
    locked = build_codex_argv(_spec(no_shell=True), "codex", "/home/agent/.codex")
    assert not any(a.startswith("--disable=") for a in plain)
    assert [a for a in locked if a.startswith("--disable=")] == [
        f"--disable={f}" for f in NO_SHELL_FEATURES]
    assert {"shell_tool", "unified_exec", "code_mode_host", "computer_use"} <= set(NO_SHELL_FEATURES)
    assert "image_generation" not in NO_SHELL_FEATURES
    assert locked[-1] == "-"          # the prompt still comes on stdin, no positional added


async def test_the_worker_marks_compare_page_jobs_no_shell(monkeypatch, tmp_path):
    from aibroker.agent.executor import ExecConfig, execute_run
    from aibroker.agent.fake import FakeAgentCLI
    from aibroker.agent.registry import clear_adapters, register_adapter
    from aibroker.agent.runs import create_run, list_events
    from aibroker.auth import generate_project_key, hash_project_key
    from aibroker.config import get_settings
    from aibroker.db import get_session
    from aibroker.db.models import DeepJobRow, ProjectRow
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=78, name="noshell", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=["llm:agent"], is_active=True, notes=""))
    monkeypatch.setattr(get_settings(), "AGENT_ALLOWED_PROJECT_IDS", "78")
    monkeypatch.setattr("aibroker.agent.executor.policy_violation", lambda *_a: None)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    seen = []
    clear_adapters()
    try:
        for labels in ({"page": "compare"}, {"page": "other"}, None):
            fake = FakeAgentCLI([ev.RunCompleted(final_text="ok")])
            register_adapter("agent:claude", lambda f=fake: f)
            rid, _ = await create_run(
                project=SimpleNamespace(id=78), capability="agent:claude", prompt="p",
                task_id="jobs", account_id="owner", session_id=None, idempotency_key=None,
                wall_timeout_s=30, idle_timeout_s=30, allowed_tools=[],
                model="claude-sonnet-5-5", labels=labels)
            async with get_session() as s:
                (await s.get(DeepJobRow, rid)).status = "running"
            outcome = await execute_run(rid, cfg)
            assert outcome == "completed", (await list_events(rid))[-1].payload
            seen.append(fake.started.no_shell)
    finally:
        clear_adapters()
    assert seen == [True, False, False]


async def test_a_no_shell_request_reaches_the_run_spec_and_the_disable_argv(monkeypatch, tmp_path):
    from aibroker.agent.executor import ExecConfig, execute_run
    from aibroker.agent.fake import FakeAgentCLI
    from aibroker.agent.registry import clear_adapters, register_adapter
    from aibroker.agent.runs import create_run
    from aibroker.auth import generate_project_key, hash_project_key
    from aibroker.config import get_settings
    from aibroker.db import get_session
    from aibroker.db.models import DeepJobRow, ProjectRow
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=79, name="noshell-req", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=["llm:agent"], is_active=True, notes=""))
    monkeypatch.setattr(get_settings(), "AGENT_ALLOWED_PROJECT_IDS", "79")
    monkeypatch.setattr("aibroker.agent.executor.policy_violation", lambda *_a: None)
    # the submit path: the request flag lands in the stored client_job
    create = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    monkeypatch.setattr(proxy, "subscription_model_verified", lambda _id: True)
    ctx = SimpleNamespace(project=SimpleNamespace(id=79, allowed_scopes=["llm:agent"]),
                          has_scope=lambda scope: True)
    for flag in (True, False):
        await proxy.jobs_submit(_chat(no_shell=flag), Response(), capability="chat:fast", ctx=ctx)
        assert create.await_args.kwargs["client_job"].get("no_shell", False) is flag
    # the worker path: the stored flag becomes AgentRunSpec.no_shell
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    seen = []
    clear_adapters()
    try:
        base = {"provider": "claude-sub", "model": "claude-sub/claude-sonnet-5-5"}
        for client_job in ({**base, "no_shell": True}, base):
            fake = FakeAgentCLI([ev.RunCompleted(final_text="ok")])
            register_adapter("agent:claude", lambda f=fake: f)
            rid, _ = await create_run(
                project=SimpleNamespace(id=79), capability="agent:claude", prompt="p",
                task_id="jobs", account_id="owner", session_id=None, idempotency_key=None,
                wall_timeout_s=30, idle_timeout_s=30, allowed_tools=[],
                model="claude-sonnet-5-5", client_job=client_job)
            async with get_session() as s:
                (await s.get(DeepJobRow, rid)).status = "running"
            assert await execute_run(rid, cfg) == "completed"
            seen.append(fake.started.no_shell)
    finally:
        clear_adapters()
    assert seen == [True, False]
    argv = build_codex_argv(_spec(no_shell=True), "codex", "/home/agent/.codex")
    assert f"--disable={NO_SHELL_FEATURES[0]}" in argv


def _chat(**extra):
    return proxy.ChatRequest.model_validate({
        "messages": [{"role": "user", "content": "draw"}],
        "model": extra.pop("model", "codex-sub/gpt-6.1-sol"), **extra})


_TOOL = {"type": "function", "function": {"name": "f", "description": "d",
                                           "parameters": {"type": "object"}}}


@pytest.mark.parametrize("model, extra", [
    ("claude-sub/claude-sonnet-5-5", {}),            # claude-sub
    ("codex-sub/gpt-6.1-sol", {"tools": [_TOOL]}),   # with tools
    ("groq/llama-3.3-70b-versatile", {}),             # not a subscription model
])
async def test_no_shell_is_a_400_outside_a_toolless_codex_job(monkeypatch, model, extra):
    from aibroker.config import get_settings
    create = AsyncMock()
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    monkeypatch.setattr(proxy, "subscription_model_verified", lambda _id: True)
    ctx = SimpleNamespace(project=SimpleNamespace(id=79, allowed_scopes=["llm:agent", "llm:chat"]),
                          has_scope=lambda scope: True)
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(_chat(model=model, no_shell=True, **extra), Response(),
                                capability="chat:fast", ctx=ctx)
    assert exc.value.status_code == 400
    assert "no_shell" in exc.value.detail
    create.assert_not_awaited()


def test_the_idempotency_hash_input_is_unchanged_without_no_shell():
    plain = _chat()
    legacy = build_request(messages=[m.model_dump(exclude_unset=True) for m in plain.messages],
                           model=plain.model, max_tokens=plain.max_tokens,
                           temperature=plain.temperature, response_format=None, workflow=None)
    assert proxy._client_request(plain) == legacy
    assert "no_shell" not in proxy._client_request(_chat(no_shell=False))
    assert proxy._client_request(_chat(no_shell=True)) == {**legacy, "no_shell": True}
