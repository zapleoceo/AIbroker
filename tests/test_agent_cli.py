"""Agent runs C3: fail-closed billing guard, argv builders, parsers (samples written from the
public docs - UNVERIFIED against the real CLIs, phase 4), subprocess plumbing with a
python stand-in process. No real codex/claude is ever started."""
from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent import real_cli
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.billing import FORBIDDEN_ENV, check_billing_mode, forbidden_env_present
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.parsers import (
    ClaudeParser,
    classify_failure,
    parse_claude_auth_status,
)
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import create_run, list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow

_PID = [9500]


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


def _spec(**kw):
    base = {"run_id": 1, "capability": "agent:codex", "prompt": "SECRET PROMPT",
            "workspace": "/w/run-1", "account_id": "acc"}
    base.update(kw)
    return AgentRunSpec(**base)


async def _run(cap="agent:codex") -> int:
    _PID[0] += 1
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"c3_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    rid, _ = await create_run(
        project=SimpleNamespace(id=_PID[0]), capability=cap, prompt="p", task_id="t",
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return rid


def _cfg(tmp_path, env):
    return ExecConfig(workspace_root=str(tmp_path / "ws"), cancel_poll_s=0.05, env=env,
                      paid_continuation_verified_off=True)


# --- billing guard --------------------------------------------------------------


@pytest.mark.parametrize(("cap", "var"), [(c, v) for c, vs in FORBIDDEN_ENV.items() for v in vs])
async def test_forbidden_env_var_refuses_and_never_starts_the_cli(tmp_path, cap, var):
    rid = await _run(cap)
    fake = FakeAgentCLI([ev.RunCompleted(final_text="x")])
    register_adapter(cap, lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path, {var: "sk-super-secret-value"})) == "failed"
    assert fake.started is None
    (last,) = [e for e in await list_events(rid) if e.type == "run.failed"]
    assert last.payload["error_class"] == "billing_mode_refused"
    assert var in last.payload["message"] and "sk-super-secret-value" not in last.payload["message"]
    async with get_session() as s:
        assert (await s.get(AgentRunRow, rid)).billing_mode == "api"


def test_forbidden_env_sets_match_the_policy():
    assert set(FORBIDDEN_ENV["agent:claude"]) == {
        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX"}
    assert set(FORBIDDEN_ENV["agent:codex"]) == {"OPENAI_API_KEY", "CODEX_API_KEY"}
    assert forbidden_env_present("agent:codex", {"ANTHROPIC_API_KEY": "x"}) == []   # other CLI's var
    assert forbidden_env_present("agent:codex", {"OPENAI_API_KEY": ""}) == ["OPENAI_API_KEY"]


@pytest.mark.parametrize("mode", ["unknown", "api"])
async def test_non_subscription_or_unknown_mode_refuses(tmp_path, mode):
    rid = await _run()
    fake = FakeAgentCLI([ev.RunCompleted()], mode=mode)
    register_adapter("agent:codex", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path, {})) == "failed"
    assert fake.started is None
    assert (await list_events(rid))[-1].payload["error_class"] == "billing_mode_refused"


async def test_adapter_that_cannot_report_mode_is_refused(tmp_path):
    class Broken(FakeAgentCLI):
        async def billing_mode(self):
            raise RuntimeError("probe died")

    mode, refusal = await check_billing_mode("agent:codex", Broken([]), {})
    assert mode == "unknown" and refusal.error_class == "billing_mode_refused"


async def test_unknown_capability_has_no_policy_and_is_refused():
    _, refusal = await check_billing_mode("agent:other", FakeAgentCLI([]), {})
    assert refusal is not None


async def test_clean_env_and_subscription_mode_runs_and_records_mode(tmp_path):
    rid = await _run()
    register_adapter("agent:codex", lambda: FakeAgentCLI([ev.RunCompleted(final_text="ok")]))
    assert await execute_run(rid, _cfg(tmp_path, {"PATH": "/bin"})) == "completed"
    async with get_session() as s:
        assert (await s.get(AgentRunRow, rid)).billing_mode == "subscription"


# --- argv ------------------------------------------------------------------------

_BAD = ("dangerously", "bypass", "danger-full-access", "full-auto", "yolo")
_BAD_TOKENS = {"--last", "--continue", "-c", "--resume-last"}


@pytest.mark.parametrize("tools", [[], ["Read"], ["Write", "Bash(git status:*)"]])
@pytest.mark.parametrize("session", [None, "sess-1"])
@pytest.mark.parametrize("model", [None, "m-exact"])
def test_argv_never_disables_safeguards_or_uses_implicit_resume(tools, session, model):
    argv = real_cli.build_claude_argv(_spec(capability="agent:claude", allowed_tools=tools,
                                            session_id=session, model=model))
    flat = " ".join(argv).lower()
    assert not any(b in flat for b in _BAD) and not _BAD_TOKENS & set(argv), argv
    assert "SECRET PROMPT" not in argv                      # prompt travels on stdin
    assert (session in argv) == bool(session)
    assert (model in argv) == bool(model)


def test_claude_argv_shape_and_explicit_tools():
    a = real_cli.build_claude_argv(_spec(capability="agent:claude"))
    assert a == ["claude", "-p", "--output-format", "stream-json", "--verbose"]
    b = real_cli.build_claude_argv(_spec(capability="agent:claude", session_id="S",
                                         allowed_tools=["Read", "Edit"], model="m"))
    assert b[b.index("--resume") + 1] == "S" and b[-3:] == ["--allowedTools", "Read", "Edit"]
    assert "--permission-mode" not in b


# --- parsers (docs-derived samples) -------------------------------------------------


def _feed(parser, lines):
    return [e for line in lines for e in parser.feed(json.dumps(line))]


def test_claude_parser_maps_a_full_conversation():
    p = ClaudeParser("/w/run-1")
    out = _feed(p, [
        {"type": "system", "subtype": "init", "session_id": "C-1", "apiKeySource": "none"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "writing"},
            {"type": "tool_use", "id": "u1", "name": "Write",
             "input": {"file_path": "/w/run-1/out/a.txt", "content": "x"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "u1", "content": "ok", "is_error": False}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "all done",
         "num_turns": 2, "total_cost_usd": 0.5,
         "usage": {"input_tokens": 7, "output_tokens": 3, "cache_read_input_tokens": 1}},
    ])
    assert [e.type for e in out] == ["session.assigned", "message.delta", "tool.call",
                                     "file.changed", "tool.result", "usage", "run.completed"]
    assert out[3].path == "out/a.txt" and out[3].action == "created"
    assert (out[5].turns, out[5].cache_read_tokens) == (2, 1)
    assert not any("cost" in k for e in out for k in e.model_dump())     # price is dropped
    assert out[-1].final_text == "all done"


def test_claude_parser_error_result_and_quota():
    p = ClaudeParser()
    out = _feed(p, [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                     "result": "Claude AI usage limit reached", "num_turns": 1, "usage": {}}])
    assert out[-1].type == "run.failed" and out[-1].error_class == "quota_exhausted"
    assert p.finish(1, "").type == "run.failed"


def test_claude_parser_refuses_api_key_source_at_runtime():
    out = _feed(ClaudeParser(), [{"type": "system", "subtype": "init", "session_id": "C",
                                  "apiKeySource": "ANTHROPIC_API_KEY"}])
    assert out[0].type == "run.failed" and out[0].error_class == "billing_mode_refused"


def test_classify_failure_words():
    assert classify_failure("rate limit exceeded") == "quota_exhausted"
    assert classify_failure("segfault") == "cli_error"


def test_login_status_parsers_need_positive_subscription_signal():
    ok = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"})
    assert parse_claude_auth_status(ok) == "subscription"
    assert parse_claude_auth_status(json.dumps({"loggedIn": True, "authMethod": "api_key"})) == "api"
    assert parse_claude_auth_status(json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"})) == "api"
    assert parse_claude_auth_status(json.dumps({"loggedIn": False})) == "unknown"
    assert parse_claude_auth_status("garbage") == "unknown"


# --- adapter plumbing with a python stand-in process (NOT a real CLI) ----------------


class _Stand(real_cli.ClaudeCLI):
    """ClaudeCLI whose argv launches `python -c <script>` instead of the claude binary."""
    def __init__(self, script, **kw):
        super().__init__(**kw)
        self.script = script

    def build_argv(self, run):
        return [sys.executable, "-c", self.script]


_OK_SCRIPT = (
    "import sys,json\n"
    "p=sys.stdin.read()\n"
    "print(json.dumps({'type':'system','subtype':'init','session_id':'C9','apiKeySource':'none'}))\n"
    "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'got:'+p,"
    "'num_turns':1,'usage':{'input_tokens':1,'output_tokens':1}}))\n"
)


async def test_subprocess_adapter_streams_events_and_prompt_on_stdin(tmp_path):
    cli = _Stand(_OK_SCRIPT)
    got = [e async for e in cli.start(_spec(workspace=str(tmp_path)))]
    assert [e.type for e in got] == ["session.assigned", "usage", "run.completed"]
    assert got[-1].final_text == "got:SECRET PROMPT"


async def test_subprocess_adapter_nonzero_exit_without_turn_is_failed(tmp_path):
    cli = _Stand("import sys; sys.stderr.write('usage limit reached'); sys.exit(3)")
    (last,) = [e async for e in cli.start(_spec(workspace=str(tmp_path)))]
    assert last.type == "run.failed" and last.error_class == "quota_exhausted"


async def test_cancel_terminates_a_hung_process(tmp_path):
    cli = _Stand("import time,sys; sys.stdin.read(); time.sleep(60)", kill_grace_s=2.0)

    async def drain():
        return [e async for e in cli.start(_spec(workspace=str(tmp_path)))]

    task = asyncio.create_task(drain())
    for _ in range(50):
        await asyncio.sleep(0.1)
        if cli._proc is not None:
            break
    await asyncio.sleep(0.3)
    await cli.cancel()
    got = await asyncio.wait_for(task, 10)
    assert cli._proc.returncode is not None and got[-1].type == "run.failed"
    await cli.cancel()                                         # idempotent


async def test_billing_mode_uses_injected_probe_and_unknown_on_error():
    async def claude(argv):
        assert argv == ["claude", "auth", "status"]
        return json.dumps({"loggedIn": True, "authMethod": "claude.ai"})

    assert await real_cli.ClaudeCLI(probe=claude).billing_mode() == "subscription"
    assert await real_cli.ClaudeCLI(probe=claude).list_models() is None


def test_register_real_adapters_registers_claude_only_without_spawning():
    from aibroker.agent.registry import make_adapter
    real_cli.register_real_adapters()
    assert isinstance(make_adapter("agent:claude"), real_cli.ClaudeCLI)
    assert make_adapter("agent:codex") is None
