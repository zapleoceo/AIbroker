"""Agent access ticked on the dashboard (projects.agent_*, migration 026): the runtime snapshot
(agent/project_access.py), how it combines with the env allowlist/policy, and the Settings-tab form."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aibroker.agent import policy as pol
from aibroker.agent import project_access as pa
from aibroker.agent.constants import project_allowed
from aibroker.agent.policy import policy_for, policy_violation
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.main import app
from tests import dash_seed as ds

client = TestClient(app)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(pa, "_snapshot", {})
    monkeypatch.setattr(get_settings(), "AGENT_ALLOWED_PROJECT_IDS", "")
    _env_policy(monkeypatch, {})


def _env_policy(monkeypatch, table: dict) -> None:
    raw = json.dumps(table)
    monkeypatch.setattr(pol, "get_settings", lambda: SimpleNamespace(AGENT_PROJECT_POLICY=raw))


async def _project(pid: int, **agent) -> None:
    async with get_session() as s:
        s.add(ds.project(pid, f"agent{pid}", **agent))


async def test_a_ticked_project_is_allowed_after_a_refresh_without_any_env():
    await _project(9701, agent_enabled=True)
    await _project(9702)
    assert not project_allowed(9701)                       # nothing loaded yet: fail closed
    await pa.refresh_access()
    assert project_allowed(9701) and not project_allowed(9702)


async def test_clearing_the_box_closes_the_project_on_the_next_refresh():
    await _project(9703, agent_enabled=True)
    await pa.refresh_access()
    async with get_session() as s:
        (await s.get(ProjectRow, 9703)).agent_enabled = False
    await pa.refresh_access()
    assert not project_allowed(9703)


async def test_env_allowlist_still_admits_a_project_with_the_box_clear(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_ALLOWED_PROJECT_IDS", "9704")
    await _project(9704)
    await pa.refresh_access()
    assert project_allowed(9704)


async def test_dashboard_policy_replaces_the_env_entry(monkeypatch):
    _env_policy(monkeypatch, {"9705": {"models": ["claude-sonnet-5-5"], "accounts": ["owner"]}})
    await _project(9705, agent_enabled=True, agent_models=["claude-opus-5-5"],
                   agent_accounts=["claude-2", "owner"])
    await pa.refresh_access()
    policy = policy_for(9705)
    assert policy.models == {"claude-opus-5-5"}
    assert policy.ordered_accounts() == ("claude-2", "owner")
    assert policy_violation(9705, "claude-sonnet-5-5", "owner") == "model_not_allowed"
    assert policy_violation(9705, "claude-opus-5-5", "claude-2") is None


async def test_without_a_dashboard_policy_the_env_entry_applies(monkeypatch):
    _env_policy(monkeypatch, {"9706": {"models": ["claude-sonnet-5-5"], "accounts": ["owner"]}})
    await _project(9706, agent_enabled=True, agent_models=["claude-opus-5-5"])   # no account
    await pa.refresh_access()
    assert policy_for(9706).models == {"claude-sonnet-5-5"}
    assert policy_for(9707) is None


async def test_a_failed_refresh_keeps_the_last_snapshot(monkeypatch):
    await _project(9708, agent_enabled=True)
    await pa.refresh_access()

    async def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(pa, "refresh_access", boom)
    stop = asyncio.Event()
    task = asyncio.create_task(pa.access_refresh_loop(stop, interval=0.01))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert project_allowed(9708)


def test_form_values_keep_only_known_models_and_accounts_in_preference_order():
    known = ["claude-opus-5-5", "gpt-6.1-sol"]
    assert pa.clean_models(["gpt-6.1-sol", "evil", "*", "gpt-6.1-sol"], known) == ("gpt-6.1-sol", "*")
    assert pa.clean_accounts(["owner", "root", "claude-2"]) == ("claude-2", "owner")


# ─── dashboard Settings tab ─────────────────────────────────────────────────


def test_settings_tab_shows_the_agent_card_with_current_boxes():
    ds.seed(ds.project(1, "stepan", agent_enabled=True, agent_accounts=["owner"]))
    body = ds.get(client, "/dashboard/projects/1", params={"tab": "settings"}).text
    assert 'action="/dashboard/projects/1/agent"' in body
    assert 'name="agent_enabled" value="1" checked' in body
    assert 'name="agent_accounts" value="owner" checked' in body
    assert 'name="agent_accounts" value="claude-2">' in body
    assert "llm:agent" in body                               # the scope warning is shown


def test_saving_the_card_stores_clean_values_and_opens_the_project_at_once():
    ds.seed(ds.project(1, "stepan", allowed_scopes=["llm:chat", "llm:agent"]))
    r = client.post("/dashboard/projects/1/agent", cookies=ds.cookies(), follow_redirects=False,
                    data={"agent_enabled": "1", "agent_models": ["*", "nope"],
                          "agent_accounts": ["owner", "claude-2", "x"],
                          "next": "/dashboard/projects/1?tab=settings"})
    assert r.status_code == 303 and "updated" in r.headers["location"]

    async def stored():
        async with get_session() as s:
            return (await s.execute(select(ProjectRow.agent_enabled, ProjectRow.agent_models,
                                           ProjectRow.agent_accounts)
                                    .where(ProjectRow.id == 1))).one()
    assert tuple(ds.run(stored())) == (True, ["*"], ["claude-2", "owner"])
    assert project_allowed(1)                                # this process refreshed on save


def test_allowed_without_models_or_accounts_warns():
    ds.seed(ds.project(1, "stepan"))
    r = client.post("/dashboard/projects/1/agent", cookies=ds.cookies(), follow_redirects=False,
                    data={"agent_enabled": "1", "next": "/dashboard/projects/1?tab=settings"})
    assert r.status_code == 303 and "agent_policy_missing" in r.headers["location"]


def test_saving_needs_the_owner_session():
    r = client.post("/dashboard/projects/1/agent", follow_redirects=False,
                    data={"agent_enabled": "1"})
    assert r.status_code in (303, 401, 403)
