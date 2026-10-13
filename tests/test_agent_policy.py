"""AGENT_PROJECT_POLICY: per-project exact-model and account allowlists, fail-closed, enforced on
create and re-checked by the worker. (The cap is metadata and untouched; no run quota.)"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent import policy as pol
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.policy import ProjectPolicy, policy_for, policy_violation
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import create_run, list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import Settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.main import app

ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-sonnet-5-5"
GOOD = {"11": {"models": [MODEL], "accounts": ["owner"]}}


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


def _policy(monkeypatch, value):
    raw = value if isinstance(value, str) else json.dumps(value)
    monkeypatch.setattr(pol, "get_settings", lambda: SimpleNamespace(AGENT_PROJECT_POLICY=raw))


async def _project(pid):
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid, name=f"pol_{pid}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    return plain


async def _post(key, **extra):
    body = {"capability": "agent:claude", "prompt": "p", "task_id": "T", "account_id": "owner",
            "model": MODEL}
    body.update(extra)
    body = {k: v for k, v in body.items() if v is not None}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/v1/agent/runs", json=body, headers={"X-Project-Key": key})


# --- parsing / fail-closed ------------------------------------------------------------------------------


def test_setting_defaults_to_empty_and_nobody_has_a_policy(monkeypatch):
    assert Settings().AGENT_PROJECT_POLICY == ""
    for raw in ("", "  ", "{}"):
        _policy(monkeypatch, raw)
        assert policy_for(11) is None and policy_violation(11, MODEL, "owner") == "agent_policy_missing"


def test_a_valid_entry_is_exact_and_ignores_unknown_keys(monkeypatch):
    _policy(monkeypatch, {"11": {"models": [MODEL, " m2 "], "accounts": ["owner"], "runs_per_day": 50}})
    p = policy_for(11)
    assert p == ProjectPolicy(models=frozenset({MODEL, "m2"}), accounts=frozenset({"owner"}))
    assert policy_for(12) is None
    assert policy_violation(11, MODEL, "owner") is None
    assert policy_violation(11, "m2", "owner") is None


@pytest.mark.parametrize("entry", [
    {"models": [], "accounts": ["owner"]}, {"models": [MODEL], "accounts": []},
    {"models": [MODEL]}, {"accounts": ["owner"]}, {}, {"models": [" "], "accounts": ["owner"]}])
def test_empty_or_missing_lists_mean_no_policy(monkeypatch, entry):
    _policy(monkeypatch, {"11": entry})
    assert policy_for(11) is None and policy_violation(11, MODEL, "owner") == "agent_policy_missing"


@pytest.mark.parametrize("raw", ["{not json", "[]", '"x"', '{"11": "nope"}', '{"abc": {}}',
                                 '{"11": {"models": "claude", "accounts": ["o"]}}',
                                 '{"11": {"models": [1], "accounts": ["o"]}}', "null"])
def test_a_malformed_setting_refuses_everyone_and_logs_once(monkeypatch, caplog, raw):
    pol._parse.cache_clear()
    _policy(monkeypatch, raw)
    with caplog.at_level(logging.ERROR, logger="aibroker.agent.policy"):
        for pid in (11, 12):
            assert policy_for(pid) is None
            assert policy_violation(pid, MODEL, "owner") == "agent_policy_missing"
    errors = [r for r in caplog.records if "AGENT_PROJECT_POLICY is malformed" in r.getMessage()]
    assert len(errors) == 1 and "EVERY agent run" in errors[0].getMessage()


def test_model_is_required_exact_and_never_implicit(monkeypatch):
    _policy(monkeypatch, GOOD)
    assert policy_violation(11, None, "owner") == "model_not_allowed"
    assert policy_violation(11, "", "owner") == "model_not_allowed"
    assert policy_violation(11, "claude-sonnet-5", "owner") == "model_not_allowed"   # no prefix match
    assert policy_violation(11, MODEL.upper(), "owner") == "model_not_allowed"
    assert policy_violation(11, "latest", "owner") == "model_not_allowed"
    assert policy_violation(11, MODEL, "other") == "account_not_allowed"
    assert policy_violation(11, MODEL, "Owner") == "account_not_allowed"


def test_wildcard_is_only_known_candidate_for_matching_agent_capability(monkeypatch):
    _policy(monkeypatch, {"11": GOOD["11"],
                          "12": {"models": ["*"], "accounts": ["owner"]}})
    assert policy_violation(12, "gpt-6-luna", "owner", "agent:codex") is None
    assert policy_violation(12, "gpt-6-luna", "owner", "agent:claude") == "model_not_allowed"
    assert policy_violation(12, "unlisted-model", "owner", "agent:codex") == "model_not_allowed"
    assert policy_violation(12, "gpt-6-luna", "other", "agent:codex") == "account_not_allowed"
    assert policy_violation(11, "gpt-6-luna", "owner", "agent:codex") == "model_not_allowed"


# --- the API --------------------------------------------------------------------------------------------------


async def test_create_refusals_carry_the_exact_error_code_and_queue_nothing(monkeypatch):
    _policy(monkeypatch, GOOD)
    key = await _project(11)
    other = await _project(12)                                     # allowlisted but no policy entry
    cases = [
        (await _post(other), "agent_policy_missing"),
        (await _post(key, model=None), "model_not_allowed"),
        (await _post(key, model="some-other-model"), "model_not_allowed"),
        (await _post(key, account_id="intruder"), "account_not_allowed"),
    ]
    for r, code in cases:
        assert r.status_code == 403 and r.json() == {"error": code}
    async with get_session() as s:
        from sqlalchemy import select
        assert (await s.execute(select(DeepJobRow))).first() is None


async def test_a_malformed_setting_is_403_for_every_project(monkeypatch):
    _policy(monkeypatch, "{oops")
    for pid in (11, 12):
        r = await _post(await _project(pid))
        assert r.status_code == 403 and r.json() == {"error": "agent_policy_missing"}


async def test_listed_model_and_account_are_accepted_and_stored(monkeypatch):
    _policy(monkeypatch, GOOD)
    key = await _project(11)
    r = await _post(key)
    assert r.status_code == 202
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        got = (await c.get(f"/v1/agent/runs/{r.json()['run_id']}", headers={"X-Project-Key": key})).json()
    assert got["model"] == MODEL and got["account_id"] == "owner"


async def test_the_scope_and_project_allowlist_checks_still_come_first(monkeypatch):
    _policy(monkeypatch, GOOD)
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=11, name="noscope", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=["llm:chat"], is_active=True, notes=""))
    r = await _post(plain)
    assert r.status_code == 403 and "lacks scope" in r.json()["detail"]


# --- the worker re-check ----------------------------------------------------------------------------------------


async def _queued(pid=11, model=MODEL, account="owner"):
    await _project(pid)
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:claude", prompt="p", task_id="t",
        account_id=account, session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model=model)
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return rid


def _cfg(tmp_path):
    return ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)


@pytest.mark.parametrize(("after", "code"), [
    ({}, "agent_policy_missing"),
    ({"11": {"models": ["another"], "accounts": ["owner"]}}, "model_not_allowed"),
    ({"11": {"models": [MODEL], "accounts": ["someone"]}}, "account_not_allowed"),
    ("{broken", "agent_policy_missing"),
])
async def test_worker_refuses_a_queued_run_when_the_policy_no_longer_allows_it(
        monkeypatch, tmp_path, after, code):
    rid = await _queued()                                         # queued under a good policy
    _policy(monkeypatch, after)
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path)) == "failed"
    assert fake.started is None
    events = await list_events(rid)
    assert [e.type for e in events] == ["run.failed"]             # no run.started, no attestation
    assert events[0].payload["error_class"] == code


async def test_worker_runs_a_run_that_fits_the_policy(monkeypatch, tmp_path):
    rid = await _queued()
    _policy(monkeypatch, GOOD)
    fake = FakeAgentCLI([ev.RunCompleted(final_text="ok")])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path)) == "completed" and fake.started.model == MODEL


async def test_a_run_without_a_model_is_refused_by_the_worker_too(monkeypatch, tmp_path):
    rid = await _queued(model=None)
    _policy(monkeypatch, GOOD)
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path)) == "failed" and fake.started is None


async def test_an_unreadable_catalog_never_waives_the_allowlist(monkeypatch, tmp_path):
    class Broken(FakeAgentCLI):
        async def list_models(self):
            raise OSError("catalog unreadable")

    # listed model + unreadable catalog -> runs (the allowlist is what decided)
    rid = await _queued()
    _policy(monkeypatch, GOOD)
    ok = Broken([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: ok)
    assert await execute_run(rid, _cfg(tmp_path)) == "completed"
    # unlisted model + unreadable catalog -> refused before the CLI (no pass-through)
    rid2 = await _queued(pid=12, model="not-listed")
    _policy(monkeypatch, {**GOOD, "12": {"models": [MODEL], "accounts": ["owner"]}})
    bad = Broken([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: bad)
    assert await execute_run(rid2, _cfg(tmp_path)) == "failed" and bad.started is None


async def test_the_catalog_check_stays_additional_when_readable(monkeypatch, tmp_path):
    from aibroker.agent.adapter import ModelInfo
    rid = await _queued()
    _policy(monkeypatch, GOOD)
    fake = FakeAgentCLI([ev.RunCompleted()], models=[ModelInfo("something-else")])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path)) == "failed" and fake.started is None
    assert (await list_events(rid))[-1].payload["error_class"] == "model_unavailable"


async def test_latest_needs_to_be_listed_explicitly(monkeypatch, tmp_path):
    rid = await _queued(model="latest")
    _policy(monkeypatch, GOOD)
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path)) == "failed" and fake.started is None
    rid2 = await _queued(pid=12, model="latest")
    _policy(monkeypatch, {"12": {"models": ["latest"], "accounts": ["owner"]}})
    fake2 = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake2)
    assert await execute_run(rid2, _cfg(tmp_path)) == "completed" and fake2.started.model == "latest"


# --- deployment -------------------------------------------------------------------------------------------------------


def test_compose_passes_the_policy_to_api_and_worker():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    line = "AGENT_PROJECT_POLICY: ${AGENT_PROJECT_POLICY:-}"
    for service in ("api", "agent-worker"):
        block = re.search(rf"^  {service}:\n(.*?)(?=^  [a-z][a-z0-9-]*:\n|^networks:|^volumes:)",
                          compose, re.S | re.M).group(1)
        assert line in block, service


def test_docs_name_the_setting_the_errors_and_the_operator_step():
    for doc in ("agent-runs.md", "api.md", "deploy-ops.md"):
        text = (ROOT / "docs" / doc).read_text(encoding="utf-8")
        assert "AGENT_PROJECT_POLICY" in text, doc
    runs = (ROOT / "docs" / "agent-runs.md").read_text(encoding="utf-8")
    for code in ("agent_policy_missing", "model_not_allowed", "account_not_allowed"):
        assert code in runs, code
    ops = (ROOT / "docs" / "deploy-ops.md").read_text(encoding="utf-8")
    assert "AGENT_PROJECT_POLICY" in ops and '"11"' in ops


async def test_run_reads_are_closed_when_the_policy_is_removed_after_queueing(monkeypatch):
    """GET /runs/{id} and /events return agent output, so they re-check the policy like scope."""
    _policy(monkeypatch, GOOD)
    key = await _project(11)
    created = await _post(key)
    assert created.status_code == 202, created.text
    rid = created.json()["run_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        for path in (f"/v1/agent/runs/{rid}", f"/v1/agent/runs/{rid}/events"):
            assert (await c.get(path, headers={"X-Project-Key": key})).status_code == 200, path
        _policy(monkeypatch, {"11": {"models": ["other-model"], "accounts": ["owner"]}})
        for path in (f"/v1/agent/runs/{rid}", f"/v1/agent/runs/{rid}/events"):
            r = await c.get(path, headers={"X-Project-Key": key})
            assert r.status_code == 403, path
            assert r.json() == {"error": "model_not_allowed"}, path


async def test_cli_model_unavailable_fails_once_with_a_clear_client_message(monkeypatch, tmp_path):
    rid = await _queued()
    _policy(monkeypatch, GOOD)
    cli_text = "There's an issue with the selected model"
    fake = FakeAgentCLI([ev.RunFailed(error_class="model_unavailable", message=cli_text)])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, _cfg(tmp_path)) == "failed"      # not parked for quota, not retried
    events = await list_events(rid)
    assert events[-1].type == "run.failed"
    assert [e.type for e in events].count("run.waiting_quota") == 0
    assert events[-1].payload["message"] == (
        f"{MODEL} is not available on this subscription account ({cli_text})")
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        assert job.error_message.startswith(
            f"model_unavailable: {MODEL} is not available on this subscription account (")
        assert job.retry_count == 0
