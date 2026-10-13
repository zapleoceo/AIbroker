"""One container per CLI, per-run HOME, redaction of every persisted secret, sandbox self-test
(fail closed), read-deny profile, and the canary-only acceptance script."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent import worker
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.codex_cli import (
    AGENT_HOME_DIR,
    PERMISSION_PROFILE,
    CodexCLI,
    permission_overrides,
)
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.parsers import CodexParser, sandbox_failure
from aibroker.agent.real_cli import register_real_adapters
from aibroker.agent.redact import REDACTED, redact, redact_bytes, redact_obj
from aibroker.agent.registry import clear_adapters, make_adapter, register_adapter
from aibroker.agent.runs import append_event, create_run, list_events
from aibroker.agent.workspace import collect_files, create_workspace
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.main import app

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
DOCS = (ROOT / "docs" / "agent-runs.md").read_text(encoding="utf-8")
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLXZhbHVl"
SK = "sk-proj-AbCdEf0123456789"
SESS = "sess-0123456789abcdefXYZ"


def _service(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|^networks:|^volumes:)", COMPOSE,
                  re.S | re.M)
    assert m, name
    return "\n".join(ln for ln in m.group(1).splitlines() if not ln.strip().startswith("#"))


def test_compose_keeps_codex_policy_and_billing_gate_separate_from_claude():
    api, claude, codex = (_service(name) for name in ("api", "agent-worker", "agent-worker-codex"))
    assert "AGENT_SUBSCRIPTION_MODELS: ${AGENT_SUBSCRIPTION_MODELS:-default}" in api
    assert "AGENT_SUBSCRIPTION_CODEX_ENABLED: ${AGENT_SUBSCRIPTION_CODEX_ENABLED:-false}" in api
    assert "AGENT_PROJECT_POLICY: ${AGENT_PROJECT_POLICY:-}" in codex
    assert "AGENT_PAID_CONTINUATION_VERIFIED_OFF: ${AGENT_PAID_CONTINUATION_VERIFIED_OFF:-false}" in claude
    assert "AGENT_CODEX_PAID_CONTINUATION_VERIFIED_OFF: ${AGENT_CODEX_PAID_CONTINUATION_VERIFIED_OFF:-false}" in codex
    assert "AGENT_PAID_CONTINUATION_VERIFIED_OFF:" not in codex
    assert "AGENT_CODEX_PAID_CONTINUATION_VERIFIED_OFF:" not in claude


def _spec(**kw):
    base = {"run_id": 1, "capability": "agent:codex", "prompt": "p", "workspace": "/w", "account_id": "a"}
    base.update(kw)
    return AgentRunSpec(**base)


@pytest.fixture(autouse=True)
def _adapters(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    clear_adapters()
    yield
    clear_adapters()


# --- one container per CLI ----------------------------------------------------------------------------


HARDENING = ("read_only: true", "init: true", "cap_drop: [ALL]", "no-new-privileges:true",
             "networks: [agent_db]",
             'profiles: ["agent"]', 'restart: "no"')


@pytest.mark.parametrize("name", ["agent-worker", "agent-worker-claude2", "agent-worker-codex"])
def test_all_workers_share_the_same_hardening(name):
    svc = _service(name)
    for line in HARDENING:
        assert line in svc, (name, line)
    assert re.search(r"- /var/lib/aibroker/agent-workspaces:size=\d+m", svc) and "- /tmp:size=" in svc
    for banned in ("docker.sock", "privileged", "network_mode", "pid: host", "devices",
                   "ANTHROPIC_", "OPENAI_", "CODEX_API"):
        assert banned not in svc, (name, banned)
    assert "DIRECT_DATABASE_URL" not in svc and "@pgbouncer:6432/" in svc
    limits = ("cpus: 2", "mem_limit: 3g", "pids_limit: 768") if name == "agent-worker-codex" else (
        "cpus: 1", "mem_limit: 1g", "pids_limit: 256")
    for line in limits:      # codex is sized for 3 parallel runs (AGENT_CONCURRENCY_CODEX)
        assert re.search(rf"^    {line}$", svc, re.M), (name, line)


def test_claude_container_claims_only_claude_and_never_mounts_the_codex_volume():
    svc = _service("agent-worker")
    assert 'AGENT_WORKER_CAPABILITIES: "agent:claude"' in svc
    assert "agent_claude_auth:/home/app/claude-state" in svc
    assert "agent_codex_auth" not in svc and "codex-state" not in svc


def test_second_claude_container_has_its_own_login_and_account():
    first, second = _service("agent-worker"), _service("agent-worker-claude2")
    assert 'AGENT_WORKER_CAPABILITIES: "agent:claude"' in second
    assert "AGENT_ACCOUNT_ID: owner" in first and "AGENT_ACCOUNT_ID: claude-2" in second
    assert "container_name: aibroker-agent-worker-claude2" in second
    assert "agent_claude2_auth:/home/app/claude-state" in second
    assert "agent_claude_auth" not in second and "agent_codex" not in second
    assert "agent_claude2" not in first
    # no shared RPC socket volume: the second worker falls back to database polling
    assert re.findall(r"^\s+- (agent_\w+):", second, re.M) == ["agent_claude2_auth"]
    assert re.findall(r"^\s+- (agent_\w+):", first, re.M) == ["agent_claude_auth", "agent_claude_rpc"]
    assert "AGENT_ACCOUNT_ID" not in _service("agent-worker-codex")
    # each Claude login has its own extra-usage confirmation; claude-2 never reads the owner's
    own = "AGENT_PAID_CONTINUATION_VERIFIED_OFF: ${AGENT_CLAUDE2_PAID_CONTINUATION_VERIFIED_OFF:-false}"
    assert own in second and "${AGENT_PAID_CONTINUATION_VERIFIED_OFF" not in second
    assert "AGENT_PAID_CONTINUATION_VERIFIED_OFF: ${AGENT_PAID_CONTINUATION_VERIFIED_OFF:-false}" in first


def test_codex_container_claims_only_codex_and_never_mounts_a_claude_volume():
    svc = _service("agent-worker-codex")
    assert 'AGENT_WORKER_CAPABILITIES: "agent:codex"' in svc
    assert "agent_codex_auth:/home/app/codex-state" in svc
    assert "agent_claude_auth" not in svc and "claude-state" not in svc.replace(
        "never the Claude state dir", "")
    vols = re.findall(r"^\s+- (agent_\w+):", svc, re.M)
    assert vols == ["agent_codex_auth", "agent_codex_rpc"]       # auth and private RPC only
    home = re.search(r"^\s+HOME: (\S+)", svc, re.M).group(1)
    assert "claude" not in home and "codex-state" not in home    # HOME is neither auth dir
    assert re.search(r'^\s+CLAUDE_CONFIG_DIR: ""', svc, re.M)    # the image default is blanked
    assert "container_name: aibroker-agent-worker-codex" in svc


def test_each_auth_volume_is_declared_once():
    for vol in ("agent_claude_auth", "agent_claude2_auth", "agent_codex_auth"):
        assert len(re.findall(rf"^  {vol}:\n", COMPOSE, re.M)) == 1


def test_worker_capabilities_setting_is_parsed_and_filtered(monkeypatch):
    def with_caps(value):
        monkeypatch.setattr(worker, "get_settings",
                            lambda: SimpleNamespace(AGENT_WORKER_CAPABILITIES=value))
        return worker.worker_capabilities()

    assert with_caps("agent:codex") == ("agent:codex",)
    assert with_caps("agent:claude") == ("agent:claude",)
    assert with_caps(" agent:claude , agent:codex ") == ("agent:codex", "agent:claude")
    assert with_caps("agent:other,evil") == () and with_caps("") == ()


def test_each_lane_needs_only_its_own_home_dir():
    marker = ROOT / "pyproject.toml"                              # any existing file stands in
    assert worker.worker_image_problem({}, str(marker), ("agent:codex",)) == "CODEX_HOME is not set"
    assert worker.worker_image_problem({"CODEX_HOME": "/c"}, str(marker), ("agent:codex",)) is None
    assert worker.worker_image_problem({"CODEX_HOME": "/c"}, str(marker), ("agent:claude",)) == \
        "CLAUDE_CONFIG_DIR is not set"
    assert worker.worker_image_problem({}, str(marker), ("agent:claude", "agent:codex")) is not None


def test_register_real_adapters_registers_only_the_lanes_capabilities():
    register_real_adapters(("agent:codex",))
    assert isinstance(make_adapter("agent:codex"), CodexCLI) and make_adapter("agent:claude") is None
    clear_adapters()
    register_real_adapters(("agent:claude",))
    assert make_adapter("agent:codex") is None and make_adapter("agent:claude") is not None


async def test_a_lane_claims_only_its_own_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        AGENT_WORKER_CAPABILITIES="agent:codex", AGENT_CONCURRENCY_CODEX=1,
        AGENT_CONCURRENCY_CLAUDE=1, AGENT_ACCOUNT_ID=""))
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=9400, name="iso", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    ids = {}
    for cap in ("agent:claude", "agent:codex"):
        ids[cap], _ = await create_run(
            project=SimpleNamespace(id=9400), capability=cap, prompt="p", task_id=cap,
            account_id="a", session_id=None, idempotency_key=None, wall_timeout_s=30,
            idle_timeout_s=30, allowed_tools=[])
        register_adapter(cap, lambda: FakeAgentCLI([ev.RunCompleted()]))
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, codex_paid_continuation_verified_off=True)
    assert await worker.drain_agent_once(cfg) == 1
    async with get_session() as s:
        assert (await s.get(DeepJobRow, ids["agent:codex"])).status == "done"
        assert (await s.get(DeepJobRow, ids["agent:claude"])).status == "pending"


async def test_a_lane_with_no_capabilities_refuses_to_start(monkeypatch):
    monkeypatch.setattr(worker, "get_settings",
                        lambda: SimpleNamespace(AGENT_WORKER_CAPABILITIES=""))
    with pytest.raises(RuntimeError, match="refused to start"):
        await worker.agent_worker_loop(asyncio.Event())


# --- per-run HOME --------------------------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory mode is checked on Linux")
def test_codex_home_for_a_run_is_a_fresh_empty_dir_inside_the_workspace(tmp_path):
    cli = CodexCLI(env={"CODEX_HOME": "/home/app/codex-state", "HOME": "/home/app/claude-state"})
    ws1, ws2 = create_workspace(str(tmp_path), 1), create_workspace(str(tmp_path), 2)
    e1 = cli.process_env(_spec(workspace=ws1))
    e2 = cli.process_env(_spec(workspace=ws2))
    assert e1["HOME"] == str(Path(ws1) / AGENT_HOME_DIR) and e1["HOME"] != e2["HOME"]
    assert e1["CODEX_HOME"] == "/home/app/codex-state"
    assert "claude" not in e1["HOME"] and e1["HOME"] != e1["CODEX_HOME"]
    assert list(Path(e1["HOME"]).iterdir()) == []
    if hasattr(stat, "S_IMODE"):
        assert stat.S_IMODE(Path(e1["HOME"]).stat().st_mode) & 0o077 == 0


# --- read-deny profile ------------------------------------------------------------------------------------------


def test_permission_profile_denies_codex_home_and_proc_and_extends_workspace():
    default, profile = permission_overrides("/home/app/codex-state")
    assert default == f'default_permissions="{PERMISSION_PROFILE}"'
    assert 'extends=":workspace"' in profile
    assert '"/home/app/codex-state"="deny"' in profile and '"/proc"="deny"' in profile
    assert "*" not in profile                                     # no glob: a /proc glob scan fails


@pytest.mark.parametrize("bad", [None, "", "relative/dir", "/a/../b", '/x"y', "/x y", "/x\n"])
def test_the_profile_refuses_an_unusable_codex_home(bad):
    with pytest.raises(ValueError, match="CODEX_HOME"):
        permission_overrides(bad)


# --- redaction -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    f"token {JWT} end", f'{{"refresh_token": "{SK}x", "other": 1}}',
    '{"access_token":"abc.def"}', '{"id_token" :  "zzz"}', "access_token=abcdef123456",
    f"key {SK}", f"id {SESS}", 'refresh_token: "r-1234567"',
])
def test_redact_masks_every_secret_shape(text):
    out = redact(text)
    assert REDACTED in out
    for secret in (JWT, SK, SESS, "abcdef123456", "r-1234567", "abc.def", "zzz"):
        assert secret not in out


def test_redact_leaves_ordinary_text_alone():
    plain = "task-1 skip-the-line session sess-ion ask sk-ip eyJ nothing here {\"a\": 1}"
    assert redact(plain) == plain


def test_redact_obj_walks_dicts_lists_and_token_keys():
    obj = {"a": [f"x {JWT}", {"refresh_token": "anything-at-all", "n": 3}], "ok": "fine"}
    out = redact_obj(obj)
    assert out["a"][0] == f"x {REDACTED}" and out["a"][1]["refresh_token"] == REDACTED
    assert out["a"][1]["n"] == 3 and out["ok"] == "fine"
    assert JWT not in json.dumps(out)


def test_redact_bytes_masks_text_and_keeps_binary():
    raw = json.dumps({"auth_mode": "chatgpt", "tokens": {"id_token": JWT}}).encode()
    assert JWT.encode() not in redact_bytes(raw)
    png = b"\x89PNG\r\n\x1a\n" + bytes(range(200, 256))
    assert redact_bytes(png) == png


async def _run(pid=9410):
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid, name=f"red_{pid}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:codex", prompt="p", task_id="t",
        account_id="a", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return plain, rid


async def test_every_persisted_event_final_text_error_and_api_response_is_redacted(tmp_path):
    key, rid = await _run()
    dirty = f"here {JWT} and {SK} and {SESS} and refresh_token=hunter22hunter22"
    register_adapter("agent:codex", lambda: FakeAgentCLI([
        ev.MessageDelta(text=dirty),
        ev.ToolCall(call_id="1", name="shell", input={"command": f"echo {SK}"}),
        ev.ToolResult(call_id="1", ok=True, output=json.dumps({"access_token": JWT})),
        ("write", "auth_copy.json", json.dumps({"id_token": JWT, "auth_mode": "chatgpt"})),
        ev.FileChanged(path="auth_copy.json", action="created"),
        ev.RunCompleted(final_text=dirty)]))
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, codex_paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"
    stored = json.dumps([e.payload for e in await list_events(rid)])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        got = (await c.get(f"/v1/agent/runs/{rid}", headers={"X-Project-Key": key})).text
        events_api = (await c.get(f"/v1/agent/runs/{rid}/events", headers={"X-Project-Key": key})).text
    files = json.loads(got)["files"]
    content = base64.b64decode(files[0]["content_b64"]).decode()
    for blob in (stored, got, events_api, content):
        for secret in (JWT, SK, SESS, "hunter22hunter22"):
            assert secret not in blob
        assert REDACTED in blob


async def test_a_failed_runs_error_text_is_redacted(tmp_path):
    key, rid = await _run(9411)
    await append_event(rid, ev.RunFailed(error_class="cli_error", message=f"boom {JWT} {SK}"))
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
    assert JWT not in job.error_message and SK not in job.error_message
    assert JWT not in json.dumps([e.payload for e in await list_events(rid)])


def test_returned_files_are_redacted_by_collect_files(tmp_path):
    ws = create_workspace(str(tmp_path), 7)
    (Path(ws) / "a.json").write_text(json.dumps({"refresh_token": SK}))
    (Path(ws) / "b.bin").write_bytes(b"\x00\x01" + SK.encode() + b"\xff\xfe")
    out = {f.path: f for f in collect_files(ws, ["a.json", "b.bin"], max_bytes=1000,
                                            total_bytes=5000, max_count=5, ttl_s=60)}
    assert SK.encode() not in base64.b64decode(out["a.json"].content_b64)
    assert out["a.json"].size == len(base64.b64decode(out["a.json"].content_b64))
    assert base64.b64decode(out["b.bin"].content_b64).startswith(b"\x00\x01")   # binary untouched


# --- sandbox self-test and failure detection (fail closed) -----------------------------------------------------------


def _probe(rc, text, seen=None):
    async def probe(argv, env, cwd):
        if seen is not None:
            seen.append((argv, dict(env), cwd))
        if isinstance(rc, Exception):
            raise rc
        return rc, text

    return probe


async def test_sandbox_preflight_runs_the_profiled_codex_sandbox_before_the_cli(tmp_path):
    seen = []
    cli = CodexCLI(env={"CODEX_HOME": "/home/app/codex-state"}, sandbox_probe=_probe(0, "", seen))
    ws = create_workspace(str(tmp_path), 1)
    assert await cli.preflight_async(_spec(workspace=ws)) is None
    argv, env, cwd = seen[0]
    assert argv[:4] == ["codex", "sandbox", "-P", PERMISSION_PROFILE] and argv[-1] == "true"
    assert any("deny" in a for a in argv) and cwd == ws and env["HOME"].endswith(AGENT_HOME_DIR)


@pytest.mark.parametrize("probe", [
    _probe(1, "bwrap: No permissions to create a new namespace, likely because the kernel"),
    _probe(0, "bwrap: Can't mount proc"), _probe(2, ""),
    _probe(0, "landlock restrict_self failed"), _probe(1, "seccomp filter not applied"),
    _probe(OSError("codex missing"), ""), _probe(124, "sandbox self-test timed out"),
])
async def test_a_failing_sandbox_selftest_fails_the_run_closed_before_any_cli_starts(tmp_path, probe):
    cli = CodexCLI(env={"CODEX_HOME": "/home/app/codex-state"}, sandbox_probe=probe)
    ws = create_workspace(str(tmp_path), 2)
    got = [e async for e in cli.start(_spec(workspace=ws))]
    assert [(e.type, e.error_class) for e in got] == [("run.failed", "sandbox_unavailable")]
    assert cli._proc is None                                      # no process was started at all


async def test_sandbox_unavailable_is_final_never_requeued_or_retried(tmp_path):
    key, rid = await _run(9412)
    register_adapter("agent:codex", lambda: FakeAgentCLI([
        ev.RunFailed(error_class="sandbox_unavailable", message="bwrap: no namespace")]))
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, codex_paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "failed"
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
    assert job.status == "error" and job.retry_count == 0
    assert job.error_message.startswith("sandbox_unavailable")
    assert "run.waiting_quota" not in [e.type for e in await list_events(rid)]


def test_sandbox_failure_wording_detection():
    for text in ("bwrap: No permissions to create a new namespace", "landlock ruleset failed",
                 "seccomp: operation not permitted", "sandbox could not be applied",
                 "Failed to apply sandbox", "user namespace creation denied"):
        assert sandbox_failure(text), text
    for text in ("", "exit status 1", "file not found", "permission denied: /etc/shadow"):
        assert not sandbox_failure(text), text


def test_parser_turns_sandbox_errors_in_events_and_stderr_into_sandbox_unavailable():
    p = CodexParser()
    out = [e for line in (
        {"type": "thread.started", "thread_id": "T"},
        {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "ls",
                                            "aggregated_output": "bwrap: No permissions to create a new namespace",
                                            "exit_code": 1, "status": "failed"}},
    ) for e in p.feed(json.dumps(line))]
    assert out[-1].type == "run.failed" and out[-1].error_class == "sandbox_unavailable"
    q = CodexParser()
    q.feed(json.dumps({"type": "turn.failed", "error": {"message": "landlock is not supported"}}))
    assert q.finish(1).error_class == "sandbox_unavailable"
    assert CodexParser().finish(1, "seccomp setup failed").error_class == "sandbox_unavailable"
    ordinary = CodexParser().feed(json.dumps({"type": "item.completed", "item": {
        "id": "2", "type": "command_execution", "command": "false", "aggregated_output": "",
        "exit_code": 1, "status": "failed"}}))
    assert [e.type for e in ordinary] == ["tool.call", "tool.result"]     # a plain failure is not it


# --- the canary-only acceptance script ---------------------------------------------------------------------------------


SCRIPT = (ROOT / "scripts" / "agent_codex_acceptance.sh").read_text(encoding="utf-8")


def test_acceptance_script_is_canary_only_and_prints_no_content():
    code = "\n".join(ln for ln in SCRIPT.splitlines() if not ln.lstrip().startswith("#"))
    assert "set -x" not in code and "echo \"$AGENT_PROJECT_KEY\"" not in code
    for real in ("cat $CODEX_HOME", "/proc/1/environ", "cat /home/app"):
        assert real not in SCRIPT.replace("/home/app/codex-state/*|/home/app/claude-state/*", "")
    assert "must contain 'canary'" in SCRIPT and "refusing: pid 1" in SCRIPT
    assert ":READABLE" in SCRIPT and ":DENIED" in SCRIPT and ">/dev/null" in SCRIPT
    assert "X-Project-Key" in code and "@\"$WORK/headers\"" in code     # key via a header file


@pytest.mark.skipif(shutil.which("bash") is None,
                    reason="no bash")
@pytest.mark.parametrize("env", [
    {"CANARY_AUTH_JSON": "/home/app/codex-state/auth.json",
     "CANARY_CLAUDE_FILE": "/t/canary/claude", "CANARY_PARENT_PID": "4242"},
    {"CANARY_AUTH_JSON": "/t/canary/auth.json", "CANARY_CLAUDE_FILE": "/home/app/claude-state/.credentials.json",
     "CANARY_PARENT_PID": "4242"},
    {"CANARY_AUTH_JSON": "/t/canary/auth.json", "CANARY_CLAUDE_FILE": "/t/canary/c",
     "CANARY_PARENT_PID": "1"},
    {"CANARY_AUTH_JSON": "/t/canary/auth.json", "CANARY_CLAUDE_FILE": "/t/canary/c",
     "CANARY_PARENT_PID": "x"},
])
def test_acceptance_script_refuses_anything_that_is_not_a_canary(env):
    full = {"AGENT_BASE_URL": "http://127.0.0.1:1", "AGENT_PROJECT_KEY": "k", "PATH": "/usr/bin:/bin",
            **env}
    res = subprocess.run(["bash", str(ROOT / "scripts" / "agent_codex_acceptance.sh")], env=full,
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 3
    assert res.stdout.strip() != "k"                              # the key is never printed


def test_docs_state_the_read_isolation_facts_and_the_canary_procedure():
    for needle in ("agent-worker-codex", "AGENT_WORKER_CAPABILITIES", "[REDACTED]", "sandbox_unavailable",
                   "deny", "default_permissions", "canary", "agent_codex_acceptance.sh",
                   "unprivileged user namespaces"):
        assert needle in DOCS, needle
