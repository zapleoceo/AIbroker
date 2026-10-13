"""AGENT_ALLOWED_PROJECT_IDS: server-side allowlist for agent runs (fail-closed when empty),
enforced on every /v1/agent/runs endpoint and re-checked by the worker before a run starts."""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent.constants import (
    AGENT_SCOPE,
    allowed_project_ids,
    project_allowed,
)
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import create_run, list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import Settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.main import app

ROOT = Path(__file__).resolve().parent.parent
DENIED = {"error": "agent_project_not_allowed"}


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


def _allow(monkeypatch, value: str):
    monkeypatch.setattr("aibroker.config.get_settings",
                        lambda: SimpleNamespace(AGENT_ALLOWED_PROJECT_IDS=value))


async def _project(pid: int, scopes=(AGENT_SCOPE,)):
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid, name=f"allow_{pid}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=list(scopes), is_active=True, notes=""))
    return plain


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


async def _call(key, method, path, **kw):
    async with _client() as c:
        return await c.request(method, f"/v1/agent/runs{path}", headers={"X-Project-Key": key}, **kw)


BODY = {"capability": "agent:claude", "prompt": "p", "task_id": "T", "account_id": "a"}


async def _every_endpoint(key, rid=1):
    return [
        await _call(key, "POST", "", json=BODY),
        await _call(key, "GET", f"/{rid}"),
        await _call(key, "GET", f"/{rid}/events"),
        await _call(key, "POST", f"/{rid}/cancel"),
    ]


# --- parsing -----------------------------------------------------------------------------------------------


def test_setting_defaults_to_empty_and_empty_means_nobody(monkeypatch):
    assert Settings().AGENT_ALLOWED_PROJECT_IDS == ""
    for value in ("", "   ", ",", "abc"):
        _allow(monkeypatch, value)
        assert allowed_project_ids() == frozenset() and not project_allowed(11)


def test_parsing_takes_integers_only_and_never_widens(monkeypatch):
    _allow(monkeypatch, " 11, 12 ,x,-3,1.5,,0013")
    assert allowed_project_ids() == frozenset({11, 12, 13})
    assert project_allowed(11) and not project_allowed(3) and not project_allowed(99)


# --- the API --------------------------------------------------------------------------------------------------


async def test_project_not_in_the_list_gets_403_on_every_endpoint(monkeypatch):
    _allow(monkeypatch, "11")
    key = await _project(12)                                    # has llm:agent, but is not listed
    for r in await _every_endpoint(key):
        assert r.status_code == 403 and r.json() == DENIED


async def test_empty_list_refuses_everyone_even_with_the_scope(monkeypatch):
    _allow(monkeypatch, "")
    for pid in (11, 12):
        key = await _project(pid)
        for r in await _every_endpoint(key):
            assert r.status_code == 403 and r.json() == DENIED


async def test_the_scope_check_still_comes_first(monkeypatch):
    _allow(monkeypatch, "11")
    key = await _project(11, scopes=["llm:chat"])               # listed, but no llm:agent
    for r in await _every_endpoint(key):
        assert r.status_code == 403 and "lacks scope" in r.json()["detail"]


async def test_listed_project_with_the_scope_works_on_every_endpoint(monkeypatch):
    _allow(monkeypatch, "11,12")
    key = await _project(11)
    created = await _call(key, "POST", "", json=BODY)
    assert created.status_code == 202
    rid = created.json()["run_id"]
    assert (await _call(key, "GET", f"/{rid}")).status_code == 200
    assert (await _call(key, "GET", f"/{rid}/events")).status_code == 200
    assert (await _call(key, "POST", f"/{rid}/cancel")).status_code == 200


async def test_a_denied_create_queues_nothing(monkeypatch):
    _allow(monkeypatch, "")
    key = await _project(11)
    assert (await _call(key, "POST", "", json=BODY)).status_code == 403
    async with get_session() as s:
        from sqlalchemy import select
        assert (await s.execute(select(DeepJobRow))).first() is None


# --- the worker re-check ---------------------------------------------------------------------------------------


async def _queued_run(pid: int):
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:claude", prompt="p", task_id="t",
        account_id="a", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return rid


def _cfg(tmp_path):
    return ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)


async def test_worker_refuses_a_queued_run_of_a_project_removed_from_the_list(monkeypatch, tmp_path):
    await _project(11)
    rid = await _queued_run(11)                                 # queued while allowed
    _allow(monkeypatch, "12")                                   # ...then removed from the list
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path)) == "failed"
    assert fake.started is None and fake.cancel_calls == 0     # no CLI start
    events = await list_events(rid)
    assert [e.type for e in events] == ["run.failed"]           # no run.started, no attestation
    assert events[0].payload["error_class"] == "project_not_allowed"
    async with get_session() as s:
        assert (await s.get(DeepJobRow, rid)).status == "error"


async def test_worker_with_an_empty_list_refuses_too(monkeypatch, tmp_path):
    await _project(11)
    rid = await _queued_run(11)
    _allow(monkeypatch, "")
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted()]))
    assert await execute_run(rid, _cfg(tmp_path)) == "failed"


async def test_worker_runs_a_listed_project(monkeypatch, tmp_path):
    await _project(11)
    rid = await _queued_run(11)
    _allow(monkeypatch, "11")
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted(final_text="ok")]))
    assert await execute_run(rid, _cfg(tmp_path)) == "completed"


# --- deployment wiring -------------------------------------------------------------------------------------------


def test_compose_passes_the_allowlist_to_api_and_worker():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    line = "AGENT_ALLOWED_PROJECT_IDS: ${AGENT_ALLOWED_PROJECT_IDS:-}"
    for service in ("api", "agent-worker"):
        block = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|^networks:|^volumes:)",
                          compose, re.S | re.M).group(1)
        assert line in block, service


def test_docs_name_the_setting_and_the_operator_step():
    for doc in ("agent-runs.md", "api.md", "deploy-ops.md"):
        text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
        assert "AGENT_ALLOWED_PROJECT_IDS" in text, doc
    assert "AGENT_ALLOWED_PROJECT_IDS=11" in (ROOT / "docs" / "deploy-ops.md").read_text(
        encoding="utf-8")
