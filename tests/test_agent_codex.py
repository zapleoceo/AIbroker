"""Codex CLI adapter: argv (no bypass flags, exact resume id), minimal env, the auth_mode-only
read, billing probe + guard + attestation, JSONL parser (doc-derived samples, UNVERIFIED against
a real run), subprocess plumbing with a python stand-in. No real codex is ever started."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec, AuthAttestation
from aibroker.agent.billing import forbidden_env_present
from aibroker.agent.codex_cli import (
    CONFIG_OVERRIDES,
    CodexCLI,
    build_codex_argv,
    build_codex_prompt,
    codex_env,
    read_auth_mode,
)
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.parsers import CodexParser, parse_codex_login_status
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.main import app

ROOT = Path(__file__).resolve().parent.parent
UUID = "123e4567-e89b-12d3-a456-426614174000"
SECRET = "sk-super-secret-token-value"


HOME_DIR = "/home/app/codex-state"


async def _sandbox_ok(argv, env, cwd):
    return 0, ""


def _argv(run):
    return build_codex_argv(run, codex_home=HOME_DIR)


def _spec(**kw):
    base = {"run_id": 1, "capability": "agent:codex", "prompt": "SECRET PROMPT",
            "workspace": "/w/run-1", "account_id": "a"}
    base.update(kw)
    return AgentRunSpec(**base)


@pytest.fixture(autouse=True)
def _adapters(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    clear_adapters()
    yield
    clear_adapters()


# --- argv ---------------------------------------------------------------------------------------------


def test_fresh_argv_shape_overrides_and_stdin_prompt():
    argv = _argv(_spec())
    assert argv[:3] == ["codex", "exec", "--json"] and argv[-1] == "-"
    assert "SECRET PROMPT" not in argv
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert tuple(pairs[:len(CONFIG_OVERRIDES)]) == CONFIG_OVERRIDES
    assert 'default_permissions="broker"' in pairs
    assert any(p.startswith("permissions={broker={extends=") for p in pairs)
    assert 'sandbox_mode="workspace-write"' in pairs and 'approval_policy="never"' in pairs
    assert 'forced_login_method="chatgpt"' in pairs and 'cli_auth_credentials_store="file"' in pairs
    for flag in ("--skip-git-repo-check", "--ignore-user-config", "--ignore-rules"):
        assert flag in argv


@pytest.mark.parametrize("session", [None, UUID])
@pytest.mark.parametrize("model", [None, "gpt-5.1-codex"])
@pytest.mark.parametrize("attach", [[], ["inputs/a.png"]])
def test_argv_never_bypasses_the_sandbox_or_approvals(session, model, attach):
    argv = _argv(_spec(session_id=session, model=model, attachments=attach))
    flat = " ".join(argv).lower()
    for banned in ("dangerously", "bypass", "yolo", "full-auto", "danger-full-access",
                   "network_access", "--last", "--oss", "--worktree", "--add-dir", "--profile"):
        assert banned not in flat, banned
    assert "-a" not in argv and "--ask-for-approval" not in argv       # no such flag on `exec`
    assert (f"--model={model}" in argv) == bool(model)                 # exact id, never substituted
    assert argv[-1] == "-" and argv[-2] == "--ignore-rules"            # prompt = stdin


def test_resume_uses_the_exact_thread_id_only():
    argv = _argv(_spec(session_id=UUID))
    assert argv[:4] == ["codex", "exec", "resume", UUID]
    for bad in ("last", "--last", "my thread name", "sess-1", UUID + "x", "../x", "-c"):
        with pytest.raises(ValueError, match="thread id"):
            _argv(_spec(session_id=bad))


def test_codex_has_no_tool_allow_list_so_any_tool_is_refused():
    with pytest.raises(ValueError, match="allowed_tools must be empty"):
        _argv(_spec(allowed_tools=["Read(src/**)"]))


def test_attachments_are_real_image_flags_before_the_other_options():
    argv = _argv(_spec(attachments=["inputs/a.png", "inputs/b.png"]))
    assert argv.index("--image=inputs/a.png") < argv.index("--json")
    assert "--image=inputs/b.png" in argv
    prompt = build_codex_prompt(_spec(attachments=["inputs/a.png"]))
    assert "inputs/a.png" in prompt and prompt.startswith("SECRET PROMPT")
    assert build_codex_prompt(_spec()) == "SECRET PROMPT"


# --- environment and auth file ----------------------------------------------------------------------------


def test_codex_env_is_an_allow_list_built_from_scratch():
    big = {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/c", "LANG": "C", "TMPDIR": "/t",
           "OPENAI_API_KEY": "k", "CODEX_API_KEY": "k", "ADMIN_KEY": "s", "DATABASE_URL": "u",
           "ANTHROPIC_API_KEY": "k", "CODEX_ACCESS_TOKEN": "t"}
    assert codex_env(big) == {"PATH": "/bin", "HOME": "/h", "CODEX_HOME": "/c", "LANG": "C",
                              "TMPDIR": "/t"}


def test_read_auth_mode_returns_only_the_mode_never_a_token(tmp_path):
    (tmp_path / "auth.json").write_text(json.dumps({
        "auth_mode": "chatgpt", "OPENAI_API_KEY": None,
        "tokens": {"id_token": SECRET, "access_token": SECRET, "refresh_token": SECRET}}))
    got = read_auth_mode(str(tmp_path))
    assert got == "chatgpt" and SECRET not in repr(got)
    assert read_auth_mode(None) is None and read_auth_mode(str(tmp_path / "nope")) is None
    (tmp_path / "auth.json").write_text("{broken")
    assert read_auth_mode(str(tmp_path)) is None
    (tmp_path / "auth.json").write_text(json.dumps({"auth_mode": {"x": SECRET}}))
    assert read_auth_mode(str(tmp_path)) is None                 # not a string -> nothing leaks
    (tmp_path / "auth.json").write_text(json.dumps([SECRET]))
    assert read_auth_mode(str(tmp_path)) is None


# --- billing probe, guard, attestation ------------------------------------------------------------------------


def _home(tmp_path, mode="chatgpt"):
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    if mode is not None:
        (home / "auth.json").write_text(json.dumps({"auth_mode": mode, "tokens": {"a": SECRET}}))
    return str(home)


def _cli(tmp_path, probe_out, mode="chatgpt", seen=None):
    async def probe(argv, env):
        if seen is not None:
            seen.append((argv, dict(env)))
        if isinstance(probe_out, Exception):
            raise probe_out
        return probe_out

    return CodexCLI(probe=probe, env={"CODEX_HOME": _home(tmp_path, mode), "PATH": "/bin",
                                      "OPENAI_API_KEY": "must-not-pass"})


async def test_chatgpt_login_in_both_places_is_a_subscription_and_attests(tmp_path):
    seen = []
    cli = _cli(tmp_path, "Logged in using ChatGPT", seen=seen)
    assert cli.attestation() is None
    assert await cli.billing_mode() == "subscription"
    att = cli.attestation()
    assert (att.auth_method, att.api_provider) == ("chatgpt", "openai-chatgpt-subscription")
    assert SECRET not in repr(att)
    argv, env = seen[0]
    assert argv[:2] == ["codex", "login"] and argv[-1] == "status"
    assert 'forced_login_method="chatgpt"' in argv and "OPENAI_API_KEY" not in env


@pytest.mark.parametrize(("out", "mode"), [
    ("Not logged in", "chatgpt"),                          # probe says no login
    ("Logged in using ChatGPT", None),                     # no auth.json
    ("Logged in using ChatGPT", "apikey"),                 # file says API key
    ("Logged in using ChatGPT", "something-new"),
    ("Logged in", "chatgpt"),                              # wording does not state ChatGPT
    ("", "chatgpt"), ("garbage", "chatgpt"),
])
async def test_anything_but_chatgpt_on_both_sides_is_not_a_subscription(tmp_path, out, mode):
    cli = _cli(tmp_path, out, mode=mode)
    assert await cli.billing_mode() != "subscription" and cli.attestation() is None


async def test_api_key_login_is_reported_as_api_and_a_failing_probe_as_unknown(tmp_path):
    assert await _cli(tmp_path, "Logged in using an API key - sk-***abc").billing_mode() == "api"
    assert await _cli(tmp_path, "x", mode="apikey").billing_mode() == "api"
    assert await _cli(tmp_path, OSError("no binary")).billing_mode() == "unknown"


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
                                  "OPENAI_ORG_ID", "OPENAI_BASE_URL", "OPENAI_ANYTHING_NEW"])
def test_openai_and_codex_key_env_is_refused_for_agent_codex(name):
    assert forbidden_env_present("agent:codex", {name: "x", "PATH": "/bin"}) == [name]


def test_codex_home_and_the_claude_names_do_not_trip_the_codex_guard():
    env = {"CODEX_HOME": "/c", "ANTHROPIC_API_KEY": "x", "PATH": "/bin"}
    assert forbidden_env_present("agent:codex", env) == []
    assert forbidden_env_present("agent:claude", {"OPENAI_API_KEY": "x"}) == []


# --- end to end on a fake codex ---------------------------------------------------------------------------------


_PID = [9600]


async def _project():
    _PID[0] += 1
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"cx_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    return plain


async def _post(key, **extra):
    body = {"capability": "agent:codex", "prompt": "p", "task_id": f"t{_PID[0]}",
            "account_id": "a", "run_mode": "exam"}
    body.update(extra)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/v1/agent/runs", json=body, headers={"X-Project-Key": key})


async def test_api_rejects_tools_and_non_uuid_resume_for_codex_only():
    key = await _project()
    assert (await _post(key, allowed_tools=["Read(src/**)"])).status_code == 422
    assert (await _post(key, session_id="sess-A")).status_code == 422
    assert (await _post(key, session_id="last")).status_code == 422
    assert (await _post(key, task_id="ok")).status_code == 202
    assert (await _post(key, capability="agent:claude", allowed_tools=["Read(src/**)"],
                        task_id="ok2")).status_code == 202


async def test_direct_codex_submission_and_queued_run_fail_closed_while_disabled(monkeypatch, tmp_path):
    key = await _project()
    rid = (await _post(key)).json()["run_id"]
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", False)
    assert (await _post(key, task_id="blocked")).status_code == 503
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    fake = FakeAgentCLI([ev.RunCompleted(final_text="must not run")])
    register_adapter("agent:codex", lambda: fake)
    assert await execute_run(rid, ExecConfig(workspace_root=str(tmp_path))) == "failed"
    assert fake.started is None
    assert (await list_events(rid))[-1].payload["error_class"] == "route_disabled"


async def test_a_codex_run_attests_chatgpt_and_reports_it(tmp_path):
    key = await _project()
    rid = (await _post(key)).json()["run_id"]
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    fake = FakeAgentCLI([ev.SessionAssigned(session_id=UUID), ev.RunCompleted(final_text="ok")])
    fake.auth = AuthAttestation("chatgpt", "openai-chatgpt-subscription")
    register_adapter("agent:codex", lambda: fake)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, codex_paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        got = (await c.get(f"/v1/agent/runs/{rid}", headers={"X-Project-Key": key})).json()
    assert (got["auth_method"], got["api_provider"], got["fallback"]) == (
        "chatgpt", "openai-chatgpt-subscription", "none")
    assert got["session_id"] == UUID


async def test_claude_paid_continuation_approval_does_not_authorize_codex(tmp_path):
    key = await _project()
    rid = (await _post(key)).json()["run_id"]
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    fake = FakeAgentCLI([ev.RunCompleted(final_text="must not run")])
    fake.auth = AuthAttestation("chatgpt", "openai-chatgpt-subscription")
    register_adapter("agent:codex", lambda: fake)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={},
                     paid_continuation_verified_off=True,
                     codex_paid_continuation_verified_off=False)
    assert await execute_run(rid, cfg) == "failed" and fake.started is None
    assert (await list_events(rid))[-1].payload["error_class"] == "billing_mode_refused"


@pytest.mark.parametrize("env", [{"OPENAI_API_KEY": "x"}, {"CODEX_API_KEY": "x"}])
async def test_a_codex_run_is_refused_when_api_env_is_present(tmp_path, env):
    key = await _project()
    rid = (await _post(key)).json()["run_id"]
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:codex", lambda: fake)
    cfg = ExecConfig(workspace_root=str(tmp_path), env=env, codex_paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "failed" and fake.started is None
    events = await list_events(rid)
    assert events[-1].payload["error_class"] == "billing_mode_refused"
    assert "billing.attested" not in [e.type for e in events]


# --- parser (docs-derived samples) -------------------------------------------------------------------------------


def _feed(parser, lines):
    return [e for line in lines for e in parser.feed(json.dumps(line))]


def test_parser_maps_a_full_turn():
    p = CodexParser()
    out = _feed(p, [
        {"type": "thread.started", "thread_id": UUID},
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"id": "i1", "type": "command_execution",
                                            "command": "ls", "aggregated_output": "a\n",
                                            "exit_code": 0, "status": "completed"}},
        {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "changes": [
            {"path": "a.txt", "kind": "add"}, {"path": "b.txt", "kind": "update"},
            {"path": "c.txt", "kind": "delete"}]}},
        {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "done"}},
        {"type": "turn.completed", "usage": {"input_tokens": 9, "cached_input_tokens": 4,
                                             "output_tokens": 2, "reasoning_output_tokens": 1}},
    ])
    assert [e.type for e in out] == ["session.assigned", "tool.call", "tool.result", "file.changed",
                                     "file.changed", "file.changed", "message.delta", "usage"]
    assert out[0].session_id == UUID and out[2].ok is True
    assert [e.action for e in out[3:6]] == ["created", "modified", "deleted"]
    u = out[-1]
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens, u.reasoning_tokens, u.turns) == (
        9, 4, 2, 1, 1)
    done = p.finish(0)
    assert done.type == "run.completed" and done.final_text == "done"


def test_parser_failures_and_quota_wording():
    p = CodexParser()
    _feed(p, [{"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}])
    f = p.finish(1)
    assert f.type == "run.failed" and f.error_class == "quota_exhausted"
    e = CodexParser()
    _feed(e, [{"type": "error", "message": "stream disconnected"}])
    assert e.finish(1).error_class == "cli_error"
    assert CodexParser().finish(2, "boom").error_class == "cli_error"
    assert CodexParser().finish(0).type == "run.failed"          # exit 0 without a finished turn
    bad = CodexParser().feed(json.dumps({"type": "item.completed", "item": {
        "id": "x", "type": "command_execution", "exit_code": 1, "command": "false"}}))
    assert bad[1].ok is False
    assert CodexParser().feed("not json") == [] and CodexParser().feed("[1]") == []


def test_login_status_parser_is_conservative():
    assert parse_codex_login_status("Logged in using ChatGPT") == "subscription"
    assert parse_codex_login_status("Logged in using an API key - sk-***abc") == "api"
    for text in ("Not logged in", "", "Logged in", "not logged in (ChatGPT)"):
        assert parse_codex_login_status(text) == "unknown"


# --- subprocess plumbing with a python stand-in (NOT the real codex) -----------------------------------------------


class _Stand(CodexCLI):
    def __init__(self, script, home, **kw):
        super().__init__(env={"CODEX_HOME": str(home), "PATH": os.environ.get("PATH", "")},
                         sandbox_probe=_sandbox_ok, **kw)
        self.script = script

    def build_argv(self, run):
        return [sys.executable, "-c", self.script]


_OK = ("import sys,json,os\n"
       "p=sys.stdin.read()\n"
       "print(json.dumps({'type':'thread.started','thread_id':'T9'}))\n"
       "print(json.dumps({'type':'item.completed','item':{'type':'agent_message',"
       "'text':'got:'+p+'|'+','.join(sorted(os.environ))}}))\n"
       "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n")


@pytest.mark.skipif(os.name == "nt", reason="Codex sandbox profile requires Linux paths")
async def test_stand_in_streams_events_prompt_on_stdin_and_a_clean_env(tmp_path, monkeypatch):
    for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "ADMIN_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(k, "leak")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "h"))
    cli = _Stand(_OK, tmp_path / "h")
    got = [e async for e in cli.start(_spec(workspace=str(tmp_path)))]
    assert [e.type for e in got] == ["session.assigned", "message.delta", "usage", "run.completed"]
    assert got[0].session_id == "T9"
    final = got[-1].final_text
    assert final.startswith("got:SECRET PROMPT|")
    assert not set(re.findall(r"[A-Z_]{4,}", final)) & {"OPENAI_API_KEY", "CODEX_API_KEY",
                                                        "ADMIN_KEY", "ANTHROPIC_API_KEY"}
    assert "CODEX_HOME" in final


async def test_preflight_refuses_without_codex_home_or_with_a_project_codex_dir(tmp_path):
    no_home = CodexCLI(env={"PATH": "/bin"})
    got = [e async for e in no_home.start(_spec(workspace=str(tmp_path)))]
    assert [(e.type, e.error_class) for e in got] == [("run.failed", "settings_refused")]
    ws = tmp_path / "ws"
    (ws / ".codex").mkdir(parents=True)
    cli = _Stand(_OK, tmp_path / "h")
    got = [e async for e in cli.start(_spec(workspace=str(ws)))]
    assert got[0].error_class == "settings_refused" and cli._proc is None


@pytest.mark.skipif(os.name == "nt", reason="Codex sandbox profile requires Linux paths")
async def test_a_bad_resume_id_fails_the_run_before_spawning(tmp_path):
    cli = CodexCLI(env={"CODEX_HOME": str(tmp_path)}, sandbox_probe=_sandbox_ok)
    got = [e async for e in cli.start(_spec(workspace=str(tmp_path), session_id="last"))]
    assert got[0].error_class == "tool_invalid" and cli._proc is None


@pytest.mark.skipif(os.name == "nt", reason="Codex sandbox profile requires Linux paths")
async def test_cancel_terminates_a_hung_stand_in(tmp_path):
    import asyncio
    cli = _Stand("import time,sys; sys.stdin.read(); time.sleep(60)", tmp_path / "h",
                 kill_grace_s=2.0)

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


# --- image / compose / docs -------------------------------------------------------------------------------------------


def test_dockerfile_installs_the_pinned_codex_with_optional_deps_and_a_codex_home():
    df = (ROOT / "infra" / "agent-worker" / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"ARG CODEX_VERSION=0\.162\.1\n", df)
    assert "npm install -g @openai/codex@${CODEX_VERSION}" in df
    assert "omit=optional" not in df.replace("never --omit=optional", "")
    assert "CODEX_HOME=/home/app/codex-state" in df and "codex --version" in df
    assert not re.search(r"(?m)^\s*(ENV|ARG)\s+(OPENAI|CODEX_API)", df)


def test_compose_has_the_codex_auth_volume_and_no_openai_env():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert "agent_codex_auth:/home/app/codex-state" in compose
    assert re.search(r"^  agent_codex_auth:\n(  #.*\n)*    driver: local", compose, re.M) or \
        re.search(r"^  agent_codex_auth:\n    driver: local", compose, re.M)
    block = compose[compose.index("\n  agent-worker:"):compose.index("\nnetworks:")]
    code = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert "OPENAI_" not in code and "CODEX_API" not in code


def test_docs_have_the_codex_section_and_login_handoff():
    docs = (ROOT / "docs" / "agent-runs.md").read_text(encoding="utf-8")
    for needle in ("CodexCLI", "codex login --device-auth", "agent_codex_auth", "auth_mode",
                   "openai-chatgpt-subscription", "device code", "no `-a` flag"):
        assert needle in docs, needle


def test_codex_parser_model_not_supported_on_account_is_model_unavailable():
    p = CodexParser()
    _feed(p, [{"type": "turn.failed", "error": {"message": (
        "The 'gpt-x' model is not supported when using Codex with a ChatGPT account.")}}])
    f = p.finish(1)
    assert f.error_class == "model_unavailable" and f.reset_at is None
    q = CodexParser()
    _feed(q, [{"type": "error", "message": "You've hit your usage limit"}])
    assert q.finish(1).error_class == "quota_exhausted"
