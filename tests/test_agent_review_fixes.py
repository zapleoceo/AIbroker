"""Independent-review blockers B1-B4 and S1-S7 (docs/agent-runs.md): tool injection, secret
inheritance, scope escape, billing env list, process group, network/fs hardening, image check."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from aibroker.agent import real_cli, worker
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.billing import forbidden_env_present
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.settings_guard import settings_problem
from aibroker.agent.tools import disallowed_tools, tool_problem, validate_tools
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.main import app

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
DOCS = (ROOT / "docs" / "agent-runs.md").read_text(encoding="utf-8")


def _service(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z-]*:\n|^networks:|^volumes:)", COMPOSE,
                  re.S | re.M)
    assert m, name
    return "\n".join(ln for ln in m.group(1).splitlines() if not ln.strip().startswith("#"))


def _spec(**kw):
    base = {"run_id": 1, "capability": "agent:claude", "prompt": "p", "workspace": "/w",
            "account_id": "a"}
    base.update(kw)
    return AgentRunSpec(**base)


# --- B1: tool allow-list / flag injection --------------------------------------------

INJECTIONS = ["--dangerously-skip-permissions", "--permission-mode=bypassPermissions",
              "--settings=/tmp/x.json", "-p", "Read,--dangerously-skip-permissions",
              "Read --settings=x", "Read(src/**),Write", "readme", "Task", "WebFetch",
              "mcp__x__y", "Read(", "Read()", "Read(a b)", "Bash", "Bash(ls)",
              "Read", "Glob", "Grep", "Edit", "Write"]


@pytest.mark.parametrize("tool", INJECTIONS)
def test_injected_or_unlisted_tools_are_refused(tool):
    assert tool_problem(tool) is not None
    with pytest.raises(ValueError):
        validate_tools([tool])
    with pytest.raises(ValueError):
        real_cli.build_claude_argv(_spec(allowed_tools=[tool]))     # never reaches argv


@pytest.mark.parametrize("tool", ["Read(src/**/*.py)", "Glob(docs/*)", "Grep(foo*)",
                                  "Edit(docs/a.md)", "Write(out/**)"])
def test_listed_tools_pass(tool):
    assert tool_problem(tool) is None


def test_argv_uses_single_joined_options_and_no_positionals():
    argv = real_cli.build_claude_argv(
        _spec(allowed_tools=["Read(docs/**)", "Grep(src/**)"], model="m-1", session_id="S-1"),
        config_dir="/c")
    assert "--allowedTools=Read(docs/**),Grep(src/**)" in argv and "--model=m-1" in argv
    assert "--resume=S-1" in argv
    assert not {"--allowedTools", "--model", "--resume", "--disallowedTools"} & set(argv)
    assert sum(a.startswith("--allowedTools") for a in argv) == 1
    assert all(a.startswith("-") for a in argv[1:] if a not in ("stream-json",))
    assert "--" not in argv      # nothing positional follows: the prompt travels on stdin


async def _project(scopes):
    plain = generate_project_key()
    pid = 7000 + len(plain) + (hash(plain) % 100000)
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid, name=f"rv_{pid}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=scopes, is_active=True, notes=""))
    return plain


async def _submit(key, **extra):
    body = {"capability": "agent:claude", "prompt": "p", "task_id": "T", "account_id": "a"}
    body.update(extra)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/v1/agent/runs", json=body, headers={"X-Project-Key": key})


@pytest.mark.parametrize("tool", INJECTIONS)
async def test_api_rejects_injected_tools(tool):
    key = await _project([AGENT_SCOPE])
    assert (await _submit(key, allowed_tools=[tool])).status_code == 422


async def test_bash_is_not_on_the_allow_list_even_for_a_scoped_project():
    key = await _project([AGENT_SCOPE, "llm:agent:bash"])
    for tool in ("Bash", "Bash(ls)", "Bash(git status:*)", "bash(ls)", "BashOutput", "KillShell"):
        assert (await _submit(key, allowed_tools=[tool])).status_code == 422, tool
        with pytest.raises(ValueError):
            real_cli.build_claude_argv(_spec(allowed_tools=[tool]))


# --- credential / environ / mount reach by task tools ----------------------------------------

REACH = ["Read(/proc/self/environ)", "Read(//proc/1/environ)", "Grep(/proc/**)",
         "Read(../claude-state/.credentials.json)",
         "Read(//home/app/claude-state/.credentials.json)", "Read(/home/app/claude-state/**)",
         "Glob(/home/app/**)", "Write(/home/app/claude-state/settings.json)",
         "Edit(//home/app/claude-state/.claude.json)", "Write(~/.claude.json)",
         "Edit(/var/lib/aibroker/agent-workspaces/other-run/x)", "Write(/etc/cron.d/x)",
         "Read(/etc/passwd)", "Glob(/**)", "Grep(//**)", "Write(../../x)", "Edit(a/../../b)"]


@pytest.mark.parametrize("tool", REACH)
async def test_task_tools_cannot_name_credentials_environ_or_mounts(tool):
    assert tool_problem(tool) is not None
    with pytest.raises(ValueError):
        real_cli.build_claude_argv(_spec(allowed_tools=[tool]))
    key = await _project([AGENT_SCOPE])
    assert (await _submit(key, allowed_tools=[tool])).status_code == 422


def test_explicit_denies_cover_credentials_proc_and_config_for_read_and_write_tools():
    deny = disallowed_tools("/home/app/claude-state")
    for tool in ("Read", "Glob", "Grep", "Edit", "Write"):
        for target in ("//proc/**", "//dev/**", "//sys/**", "**/.credentials.json", "**/.claude.json",
                       "//home/app/claude-state/**"):
            assert f"{tool}({target})" in deny, (tool, target)


async def test_adapter_reports_tool_invalid_without_spawning(tmp_path):
    cli = real_cli.ClaudeCLI(env={"CLAUDE_CONFIG_DIR": str(tmp_path)})
    got = [e async for e in cli.start(_spec(workspace=str(tmp_path),
                                            allowed_tools=["--dangerously-skip-permissions"]))]
    assert [(e.type, e.error_class) for e in got] == [("run.failed", "tool_invalid")]
    assert cli._proc is None


# --- B2: minimal environment ------------------------------------------------------------


def test_cli_env_keeps_only_the_minimal_keys():
    big = {"PATH": "/bin", "HOME": "/h", "CLAUDE_CONFIG_DIR": "/c", "LANG": "C", "TMPDIR": "/t",
           "ADMIN_KEY": "s", "INTERNAL_SECRET": "s", "TOKEN_SECRET": "s",
           "DATABASE_URL": "postgresql://u:p@h/d", "ANTHROPIC_API_KEY": "k"}
    env = real_cli.cli_env(big)
    assert set(env) == {"PATH", "HOME", "CLAUDE_CONFIG_DIR", "LANG", "TMPDIR",
                        "DISABLE_AUTOUPDATER"}
    assert env["DISABLE_AUTOUPDATER"] == "1"


async def test_subprocess_and_probe_never_see_worker_secrets(tmp_path, monkeypatch):
    for k in ("ADMIN_KEY", "INTERNAL_SECRET", "DATABASE_URL"):
        monkeypatch.setenv(k, "very-secret")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    alt = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
           "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK",
           "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_CUSTOM_HEADERS")
    for k in alt:
        monkeypatch.setenv(k, "alt-provider")
    seen = {}

    async def probe(argv, env):
        seen.update(env)
        return json.dumps({"loggedIn": True, "authMethod": "claude.ai"})

    cli = real_cli.ClaudeCLI(probe=probe)
    assert await cli.billing_mode() == "subscription"
    assert "ADMIN_KEY" not in seen and "DATABASE_URL" not in seen and not set(alt) & set(seen)

    script = ("import os,json;print(json.dumps({'type':'result','subtype':'success',"
              "'is_error':False,'result':','.join(sorted(os.environ)),'usage':{}}))")

    class Stand(real_cli.ClaudeCLI):
        def build_argv(self, run):
            return [sys.executable, "-c", script]

    got = [e async for e in Stand().start(_spec(workspace=str(tmp_path)))]
    keys = set(got[-1].final_text.split(","))
    assert not keys & {"ADMIN_KEY", "INTERNAL_SECRET", "DATABASE_URL", *alt}
    assert keys <= set(real_cli.CLI_ENV_KEYS) | {"SYSTEMROOT", "LC_CTYPE", "PWD", "SHLVL", "_"}


def test_compose_worker_has_no_real_broker_secrets():
    svc = _service("agent-worker")
    for name in ("ADMIN_KEY", "INTERNAL_SECRET", "TOKEN_SECRET", "SESSION_SECRET"):
        line = next(ln for ln in svc.splitlines() if ln.strip().startswith(f"{name}:")) \
            if f"{name}:" in svc else ""
        assert "${" not in line, name
    assert "placeholder" in svc


# --- B3: scope escape / settings files ------------------------------------------------------


@pytest.mark.parametrize("tool", ["Read(/etc/passwd)", "Read(//etc/passwd)", "Write(~/x)",
                                  "Edit(../x)", "Grep(a/../../b)", "Glob(C:/x)", "Read(\\\\x)"])
def test_absolute_and_dotdot_scopes_are_refused(tool):
    assert tool_problem(tool) is not None


def test_proc_and_config_dir_are_always_disallowed():
    d = disallowed_tools("/home/app/claude-state")
    assert "Read(//proc/**)" in d and "Write(//proc/**)" in d
    assert "Read(//home/app/claude-state/**)" in d and "Edit(//home/app/claude-state/**)" in d
    assert all("," not in x for x in d)
    argv = real_cli.build_claude_argv(_spec(), config_dir="/home/app/claude-state")
    assert any(a.startswith("--disallowedTools=") and "//proc/**" in a for a in argv)
    assert "--setting-sources=user" in argv


@pytest.mark.parametrize("content", [
    {"apiKeyHelper": "/bin/x"}, {"env": {"ANTHROPIC_API_KEY": "k"}},
    {"permissions": {"defaultMode": "acceptEdits"}},
    {"permissions": {"allow": ["x"], "defaultMode": "bypassPermissions"}},
    {"nested": [{"deep": "bypassPermissions"}]},
])
@pytest.mark.parametrize("where", ["cfg/settings.json", "cfg/settings.local.json",
                                   "ws/.claude/settings.json", "ws/.claude/settings.local.json"])
def test_dirty_settings_files_fail_closed(tmp_path, content, where):
    for d in ("cfg", "ws/.claude"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / where).write_text(json.dumps(content))
    assert settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws")) is not None


def test_clean_missing_broken_and_unset_settings(tmp_path):
    (tmp_path / "cfg").mkdir()
    assert settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws")) is None      # no files
    (tmp_path / "cfg" / "settings.json").write_text(json.dumps(
        {"theme": "dark", "verbose": True}))
    assert settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws")) is None
    (tmp_path / "cfg" / "settings.local.json").write_text("{not json")
    assert "unreadable" in settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws"))
    assert settings_problem(None, "/w") == "CLAUDE_CONFIG_DIR is not set"


async def test_adapter_refuses_dirty_settings_before_spawning(tmp_path):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "settings.json").write_text(json.dumps({"apiKeyHelper": "x"}))
    cli = real_cli.ClaudeCLI(env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")})
    got = [e async for e in cli.start(_spec(workspace=str(tmp_path)))]
    assert [(e.type, e.error_class) for e in got] == [("run.failed", "settings_refused")]
    assert cli._proc is None


# --- B4: billing env list ------------------------------------------------------------------


@pytest.mark.parametrize("name", [
    "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_USE_FOUNDRY", "AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_SOMETHING_NEW", "CLAUDE_CODE_USE_NEWCLOUD"])
def test_extended_claude_billing_env_is_refused(name):
    assert forbidden_env_present("agent:claude", {name: "x", "PATH": "/bin"}) == [name]


def test_prefix_block_is_claude_specific_and_value_free():
    assert forbidden_env_present("agent:codex", {"ANTHROPIC_FOO": "x"}) == []
    assert forbidden_env_present("agent:claude", {"CLAUDE_CODE_FOO": "x"}) == []


# --- S1: process group ------------------------------------------------------------------------


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return False
    return "State:\tZ" not in state


@pytest.mark.skipif(not hasattr(os, "killpg") or not Path("/proc/self").exists(),
                    reason="POSIX process groups + /proc")
async def test_cancel_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time\n"
        f"c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"open({str(pidfile)!r},'w').write(str(c.pid))\n"
        "sys.stdin.read();time.sleep(60)\n")

    class Stand(real_cli.ClaudeCLI):
        def build_argv(self, run):
            return [sys.executable, "-c", script]

    cli = Stand(env={"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg")}, kill_grace_s=2.0)
    task = asyncio.create_task(_drain(cli, str(tmp_path)))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if pidfile.exists() and pidfile.read_text():
            break
    child = int(pidfile.read_text())
    assert _alive(child)
    await cli.cancel()
    await asyncio.wait_for(task, 10)
    for _ in range(50):
        if not _alive(child):
            break
        await asyncio.sleep(0.1)
    assert not _alive(child)


async def _drain(cli, ws):
    return [e async for e in cli.start(_spec(workspace=ws))]


# --- S3 / S4: compose hardening ---------------------------------------------------------------


def test_agent_worker_init_readonly_tmpfs_and_dedicated_network():
    svc = _service("agent-worker")
    assert "init: true" in svc and "read_only: true" in svc
    assert re.search(r"- /tmp:size=\d+m", svc)
    assert "networks: [agent_db]" in svc and "default" not in svc.split("networks:")[1]


def test_only_the_worker_is_declared_on_the_agent_network():
    on = {n for n in ("postgres", "pgbouncer", "redis", "api", "monitor", "vision-local")
          if "agent_db" in _service(n)}
    assert on == set()                            # shared DB is never recreated by a deploy
    for n in ("postgres", "pgbouncer"):
        assert "networks:" not in _service(n)     # their networks are unchanged
    assert re.search(r"^networks:\n(  #.*\n)*  agent_db:\n    driver: bridge", COMPOSE, re.M)
    assert "internal: true" not in COMPOSE      # internet egress stays (documented accepted risk)


def test_docs_give_the_live_network_attach_commands():
    for svc, ctr in (("postgres", "aibroker-postgres"), ("pgbouncer", "aibroker-pgbouncer")):
        assert f"docker network connect --alias {svc} aibroker_agent_db {ctr}" in DOCS
    assert re.search(r"^name: aibroker$", COMPOSE, re.M)          # => network aibroker_agent_db


# --- S5: refuse outside the worker image ------------------------------------------------------


def test_worker_image_check(tmp_path):
    marker = tmp_path / "marker"
    assert "CLAUDE_CONFIG_DIR" in worker.worker_image_problem({}, str(marker))
    assert "missing" in worker.worker_image_problem({"CLAUDE_CONFIG_DIR": "/c"}, str(marker))
    marker.write_text("x")
    assert worker.worker_image_problem({"CLAUDE_CONFIG_DIR": "/c"}, str(marker)) is None
    assert worker.worker_image_problem({}, str(marker)) is not None


async def test_agent_lane_refuses_to_start_outside_the_image():
    with pytest.raises(RuntimeError, match="refused to start"):
        await worker.agent_worker_loop(asyncio.Event())


def test_dockerfile_writes_the_marker():
    df = (ROOT / "infra" / "agent-worker" / "Dockerfile").read_text(encoding="utf-8")
    assert worker.WORKER_MARKER in df and "HOME=/home/app/claude-state" in df


# --- S6 / S7 / docs -----------------------------------------------------------------------------


def test_docs_cover_checklist_login_persistence_and_accepted_risks():
    assert "docker compose --profile agent config | grep -i anthropic" in DOCS
    assert "persists" in DOCS and "named volume" in DOCS
    for needle in ("Bash", "accepted risk", "/proc", "uid", "guaranteed", "first supervised run",
                   "/dev/fd/", "NOT recreated"):
        assert needle in DOCS, needle


# --- round 2: mandatory scope, --tools, MCP, dumpable, settings allow-list ------------------------


@pytest.mark.parametrize("tool", ["Read", "Glob", "Grep", "Edit", "Write"])
async def test_bare_file_tools_are_refused_everywhere(tool):
    assert tool_problem(tool) is not None
    with pytest.raises(ValueError):
        real_cli.build_claude_argv(_spec(allowed_tools=[tool]))
    key = await _project([AGENT_SCOPE])
    assert (await _submit(key, allowed_tools=[tool])).status_code == 422


def test_attachment_runs_get_only_a_scoped_read():
    argv = real_cli.build_claude_argv(_spec(attachments=["inputs/a.png"]))
    allowed = next(a for a in argv if a.startswith("--allowedTools="))
    assert allowed == "--allowedTools=Read(inputs/**)"
    assert "Read" not in allowed.removeprefix("--allowedTools=").split(",")   # never bare
    both = real_cli.build_claude_argv(_spec(attachments=["inputs/a.png"],
                                            allowed_tools=["Grep(src/**)"]))
    assert "--allowedTools=Grep(src/**),Read(inputs/**)" in both
    again = real_cli.build_claude_argv(_spec(attachments=["inputs/a.png"],
                                             allowed_tools=["Read(inputs/**)"]))
    assert "--allowedTools=Read(inputs/**)" in again


def test_tools_flag_restricts_the_builtin_set_and_mcp_is_off():
    argv = real_cli.build_claude_argv(_spec(allowed_tools=["Read(a/**)", "Read(b/**)", "Edit(c/*)"]))
    assert "--tools=Read,Edit" in argv and "--strict-mcp-config" in argv
    assert "--mcp-config" not in " ".join(argv)
    none = real_cli.build_claude_argv(_spec())
    assert none[none.index("--tools") + 1] == "" and "--strict-mcp-config" in none


def test_dev_and_sys_are_denied_too():
    deny = disallowed_tools("/c")
    for tool in ("Read", "Glob", "Grep", "Edit", "Write"):
        assert f"{tool}(//dev/**)" in deny and f"{tool}(//sys/**)" in deny


def test_worker_makes_itself_non_dumpable():
    from aibroker.agent.run_worker import PR_SET_DUMPABLE, set_non_dumpable
    calls = []

    class Libc:
        def prctl(self, *args):
            calls.append(args)
            return 0

    assert set_non_dumpable(Libc()) is True
    assert calls == [(PR_SET_DUMPABLE, 0, 0, 0, 0)] and PR_SET_DUMPABLE == 4

    class Failing:
        def prctl(self, *args):
            return -1

    assert set_non_dumpable(Failing()) is False
    assert set_non_dumpable(object()) is False          # no prctl at all (non-Linux)


def test_run_worker_calls_set_non_dumpable_before_touching_the_database():
    import inspect

    from aibroker.agent import run_worker
    src = inspect.getsource(run_worker.run_worker)
    assert src.index("set_non_dumpable") < src.index("init_engine")


@pytest.mark.parametrize("content", [
    {"hooks": {"PreToolUse": []}}, {"permissions": {"allow": ["Read(*)"]}},
    {"statusLine": {"type": "command", "command": "x"}}, {"mcpServers": {"x": {}}},
    {"enableAllProjectMcpServers": True}, {"otelHeadersHelper": "x"}, {"awsAuthRefresh": "x"},
    {"awsCredentialExport": "x"}, {"forceLoginMethod": "console"}, {"apiKeyHelper": "x"},
    {"env": {"A": "b"}}, {"defaultMode": "x"}, {"model": "opus"}, {"theme": {"nested": 1}},
    ["not", "an", "object"],
])
def test_settings_allow_list_refuses_everything_else(tmp_path, content):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "settings.json").write_text(json.dumps(content))
    assert settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws")) is not None


def test_settings_allow_list_accepts_inert_keys_and_login_state(tmp_path):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / "settings.json").write_text(json.dumps(
        {"$schema": "x", "theme": "dark", "verbose": False}))
    (tmp_path / "cfg" / ".claude.json").write_text(json.dumps(
        {"userID": "u", "oauthAccount": {"emailAddress": "o@example.com"},
         "projects": {"/w": {"allowedTools": []}}}))
    (tmp_path / "cfg" / ".credentials.json").write_text("{not even read}")
    assert settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws")) is None


@pytest.mark.parametrize("state", [{"mcpServers": {"x": {}}},
                                   {"projects": {"/w": {"mcpServers": {"x": {}}}}}])
def test_mcp_servers_in_claude_json_refuse_the_run(tmp_path, state):
    (tmp_path / "cfg").mkdir()
    (tmp_path / "cfg" / ".claude.json").write_text(json.dumps(state))
    assert "mcpServers" in settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws"))
    (tmp_path / "cfg" / ".claude.json").write_text("{broken")
    assert settings_problem(str(tmp_path / "cfg"), str(tmp_path / "ws")) is not None
