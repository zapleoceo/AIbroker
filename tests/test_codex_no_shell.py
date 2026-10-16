"""/ask2many Codex runs get no command tools (AgentRunSpec.no_shell -> codex --disable ...)."""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.codex_cli import NO_SHELL_FEATURES, build_codex_argv


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
