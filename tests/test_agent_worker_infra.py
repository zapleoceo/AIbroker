"""Agent-worker container policy + the paid-continuation billing gate (docs/agent-runs.md)."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent.billing import check_billing_mode, paid_continuation_refusal
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, exec_config_from_settings, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import create_run, list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import Settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
DOCKERFILE = (ROOT / "infra" / "agent-worker" / "Dockerfile").read_text(encoding="utf-8")


def _service(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z-]*:\n|^volumes:)", COMPOSE, re.S | re.M)
    assert m, name
    return m.group(1)


# --- compose / Dockerfile policy ---------------------------------------------------


def test_agent_worker_is_behind_the_agent_profile_only():
    assert 'profiles: ["agent"]' in _service("agent-worker")
    for other in ("api", "monitor", "postgres", "redis"):
        assert "profiles" not in _service(other)


def test_agent_worker_has_no_privilege_expansion():
    svc = _service("agent-worker")
    code = "\n".join(line for line in svc.splitlines() if not line.strip().startswith("#"))
    code = code.replace("/home/app/claude-state", "STATE").replace("/home/app/codex-state", "STATE2")
    for banned in ("docker.sock", "privileged", "network_mode", "pid: host", "/home/", "devices",
                   "ANTHROPIC_", "OPENAI_", "CODEX_API"):
        assert banned not in code, banned
    assert "agent_claude_auth:STATE" in code and "agent_codex_auth:STATE2" in code and "cap_drop: [ALL]" in code
    assert "no-new-privileges:true" in code
    assert re.search(r"cpus: 1\b", code) and "mem_limit: 1g" in code and "pids_limit: 256" in code
    assert "NEEDS OWNER OK" in svc and "AGENT_PAID_CONTINUATION_VERIFIED_OFF" in svc
    assert 'restart: "no"' in code


def test_named_auth_volume_is_declared():
    assert re.search(r"^  agent_claude_auth:\n    driver: local", COMPOSE, re.M)


def test_dockerfile_pins_cli_uses_node22_and_runs_non_root():
    m = re.search(r"ARG CLAUDE_CODE_VERSION=(\d+)\.(\d+)\.(\d+)\n", DOCKERFILE)
    assert m and tuple(map(int, m.groups())) >= (2, 1, 277)
    assert "npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" in DOCKERFILE
    assert "FROM node:22-slim" in DOCKERFILE and "@latest" not in DOCKERFILE
    assert DOCKERFILE.strip().splitlines().count("USER app") == 1
    assert DOCKERFILE.index("USER root") < DOCKERFILE.index("USER app")
    assert "CLAUDE_CONFIG_DIR=/home/app/claude-state" in DOCKERFILE
    assert "aibroker.agent.run_worker" in DOCKERFILE
    assert not re.search(r"(?m)^\s*(ENV|ARG)\s+(ANTHROPIC|OPENAI|CODEX_API)", DOCKERFILE)


# --- paid-continuation gate ----------------------------------------------------------


def test_setting_defaults_to_not_verified():
    assert Settings().AGENT_PAID_CONTINUATION_VERIFIED_OFF is False
    assert exec_config_from_settings().paid_continuation_verified_off is False


@pytest.mark.parametrize(("mode", "ack", "off", "refused"), [
    ("autonomous", False, False, True), ("exam", True, False, True),
    ("autonomous", True, False, True), ("manual", False, False, True),
    ("manual", True, False, False),
    ("autonomous", False, True, False), ("exam", False, True, False), ("manual", False, True, False),
])
def test_paid_continuation_matrix(mode, ack, off, refused):
    r = paid_continuation_refusal(mode, ack, off)
    assert (r is not None) is refused
    if r:
        assert r.error_class == "billing_mode_refused"


async def test_guard_checks_env_and_mode_before_the_paid_gate():
    _, r = await check_billing_mode("agent:claude", FakeAgentCLI([], mode="unknown"), {},
                                    run_mode="manual", risk_ack=True)
    assert "subscription mode" in r.message
    _, r = await check_billing_mode("agent:claude", FakeAgentCLI([]), {"ANTHROPIC_API_KEY": "x"},
                                    verified_off=True)
    assert "ANTHROPIC_API_KEY" in r.message


_PID = [9900]


async def _run(**kw):
    _PID[0] += 1
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"inf_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    rid, _ = await create_run(
        project=SimpleNamespace(id=_PID[0]), capability="agent:claude", prompt="p", task_id="t",
        account_id="a", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], **kw)
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return rid


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


@pytest.mark.parametrize("kw", [
    {}, {"run_mode": "exam"}, {"run_mode": "manual"},
    {"run_mode": "autonomous", "accept_paid_continuation_risk": True},
])
async def test_unverified_runs_are_refused_before_the_cli_starts(tmp_path, kw):
    rid = await _run(**kw)
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={})          # verified_off defaults False
    assert await execute_run(rid, cfg) == "failed" and fake.started is None
    last = (await list_events(rid))[-1]
    assert last.payload["error_class"] == "billing_mode_refused"
    assert "paid continuation" in last.payload["message"]


async def test_manual_run_with_explicit_ack_is_allowed(tmp_path):
    rid = await _run(run_mode="manual", accept_paid_continuation_risk=True)
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted(final_text="ok")]))
    assert await execute_run(rid, ExecConfig(workspace_root=str(tmp_path), env={})) == "completed"


async def test_verified_off_allows_autonomous_runs(tmp_path):
    rid = await _run(run_mode="exam")
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted()]))
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"
