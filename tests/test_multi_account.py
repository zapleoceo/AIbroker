"""Multi-account Claude subscription runs: per-account worker claim, account selection for
/v1/jobs (policy order, quota pauses), same-account quota waits, account in the poll."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent import policy as pol
from aibroker.agent import worker
from aibroker.agent.accounts import pick_account, quota_resets
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.policy import ProjectPolicy, policy_for
from aibroker.agent.runs import append_event, create_run, get_attestation, get_run, park_for_quota
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.main import app
from aibroker.providers.catalog import listing
from aibroker.providers.subscriptions import subscription_model_verified
from aibroker.routes import proxy

CAP = "agent:claude"
MODEL = "claude-sonnet-5-5"
_PID = [9500]


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _project(scopes=(AGENT_SCOPE, "llm:chat")) -> tuple[int, str]:
    _PID[0] += 1
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"acct_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=list(scopes), is_active=True, notes=""))
    return _PID[0], plain


async def _run(pid: int, account: str, task: str = "T") -> int:
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=CAP, prompt="p", task_id=task,
        account_id=account, session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model=MODEL,
        client_job={"provider": "claude-sub", "model": f"claude-sub/{MODEL}",
                    "response_format": None})
    return rid


def _worker_settings(monkeypatch, account: str) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(AGENT_ACCOUNT_ID=account))


def _policy(monkeypatch, accounts: list[str], models=(MODEL,)) -> None:
    raw = json.dumps({str(pid): {"models": list(models), "accounts": accounts}
                      for pid in range(9501, 9700)})
    monkeypatch.setattr(pol, "get_settings", lambda: SimpleNamespace(AGENT_PROJECT_POLICY=raw))


# --- worker claim -------------------------------------------------------------------------------


async def test_claim_is_isolated_by_account(monkeypatch):
    pid, _ = await _project()
    owner = await _run(pid, "owner")
    second = await _run(pid, "claude-2")
    _worker_settings(monkeypatch, "claude-2")
    assert await worker.claim_agent_runs(CAP, 5) == [second]
    _worker_settings(monkeypatch, "owner")
    assert await worker.claim_agent_runs(CAP, 5) == [owner]
    async with get_session() as s:
        assert (await s.get(DeepJobRow, owner)).status == "running"
        assert (await s.get(DeepJobRow, second)).status == "running"


async def test_claim_for_one_account_leaves_the_other_pending(monkeypatch):
    pid, _ = await _project()
    owner = await _run(pid, "owner")
    _worker_settings(monkeypatch, "claude-2")
    assert await worker.claim_agent_runs(CAP, 5) == []
    async with get_session() as s:
        assert (await s.get(DeepJobRow, owner)).status == "pending"


async def test_empty_account_setting_claims_every_account(monkeypatch):
    pid, _ = await _project()
    ids = [await _run(pid, "owner"), await _run(pid, "claude-2")]
    _worker_settings(monkeypatch, "")
    assert sorted(await worker.claim_agent_runs(CAP, 5)) == sorted(ids)


# --- selection ----------------------------------------------------------------------------------


async def test_first_candidate_is_used_when_nothing_is_paused():
    assert await pick_account(("owner", "claude-2"), CAP) == "owner"
    assert await pick_account((), CAP) is None


async def test_selection_skips_a_paused_account():
    pid, _ = await _project()
    await park_for_quota(await _run(pid, "owner"), _now() + timedelta(hours=2))
    assert await pick_account(("owner", "claude-2"), CAP) == "claude-2"
    assert await pick_account(("claude-2", "owner"), CAP) == "claude-2"


async def test_all_paused_picks_the_earliest_reset():
    pid, _ = await _project()
    await park_for_quota(await _run(pid, "owner"), _now() + timedelta(hours=3))
    await park_for_quota(await _run(pid, "claude-2"), _now() + timedelta(hours=1))
    assert await pick_account(("owner", "claude-2"), CAP) == "claude-2"


async def test_an_expired_pause_no_longer_counts():
    pid, _ = await _project()
    await park_for_quota(await _run(pid, "owner"), _now() - timedelta(minutes=1))
    assert await pick_account(("owner", "claude-2"), CAP) == "owner"


async def test_latest_quota_signal_of_an_account_decides():
    pid, _ = await _project()
    rid = await _run(pid, "owner")
    await park_for_quota(rid, _now() + timedelta(hours=5))
    await park_for_quota(rid, _now() - timedelta(minutes=1))     # a later wait that already ended
    assert await quota_resets(["owner"], CAP) == {}


async def test_quota_exhausted_failure_with_future_reset_pauses_the_account():
    pid, _ = await _project()
    reset = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    await append_event(await _run(pid, "owner"),
                       ev.RunFailed(error_class="quota_exhausted", reset_at=reset))
    assert await pick_account(("owner", "claude-2"), CAP) == "claude-2"
    other = await _run(pid, "claude-2")
    await append_event(other, ev.RunFailed(error_class="cli_error"))    # not a quota signal
    assert await pick_account(("owner", "claude-2"), CAP) == "claude-2"


async def test_other_capability_quota_does_not_pause_the_account():
    pid, _ = await _project()
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:codex", prompt="p", task_id="T",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    await park_for_quota(rid, _now() + timedelta(hours=2))
    assert await pick_account(("owner", "claude-2"), CAP) == "owner"


def test_policy_keeps_the_listed_account_order(monkeypatch):
    _policy(monkeypatch, ["claude-2", "owner", "claude-2"])
    assert policy_for(9501).ordered_accounts() == ("claude-2", "owner")
    assert ProjectPolicy(frozenset({"m"}), frozenset({"b", "a"})).ordered_accounts() == ("a", "b")


# --- /v1/jobs -----------------------------------------------------------------------------------


def _submit(plain: str):
    return TestClient(app).post(
        "/v1/jobs?capability=chat:fast", headers={"X-Project-Key": plain},
        json={"model": f"claude-sub/{MODEL}", "messages": [{"role": "user", "content": "hi"}]})


async def _account_of(job_id: int) -> str:
    async with get_session() as s:
        return (await s.get(AgentRunRow, job_id)).account_id


@pytest.fixture
def _jobs_env(monkeypatch):
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS", "default")


async def test_jobs_submit_policy_restricts_the_candidates(monkeypatch, _jobs_env):
    _, plain = await _project()
    _policy(monkeypatch, ["claude-2"])
    r = _submit(plain)
    assert r.status_code == 202, r.text
    assert await _account_of(r.json()["job_id"]) == "claude-2"


async def test_jobs_submit_follows_policy_order_then_skips_a_paused_account(monkeypatch, _jobs_env):
    _, plain = await _project()
    _policy(monkeypatch, ["owner", "claude-2"])
    first = _submit(plain)
    first_id = first.json()["job_id"]
    assert await _account_of(first_id) == "owner"
    await park_for_quota(first_id, _now() + timedelta(hours=2))
    second = _submit(plain)
    assert second.status_code == 202, second.text
    assert await _account_of(second.json()["job_id"]) == "claude-2"
    assert await _account_of(first_id) == "owner"     # the parked run never moves


async def test_jobs_submit_without_policy_is_still_refused(monkeypatch, _jobs_env):
    _, plain = await _project()
    monkeypatch.setattr(pol, "get_settings", lambda: SimpleNamespace(AGENT_PROJECT_POLICY=""))
    r = _submit(plain)
    assert r.status_code == 403 and r.json()["error"] == "agent_policy_missing"


async def test_started_run_keeps_its_account_through_a_quota_wait(monkeypatch):
    pid, _ = await _project()
    rid = await _run(pid, "owner")
    _worker_settings(monkeypatch, "owner")
    assert await worker.claim_agent_runs(CAP, 1) == [rid]
    await park_for_quota(rid, _now() - timedelta(seconds=1))      # parked, retry due now
    _worker_settings(monkeypatch, "claude-2")
    assert await worker.claim_agent_runs(CAP, 1) == []            # the other account cannot take it
    _worker_settings(monkeypatch, "owner")
    assert await worker.claim_agent_runs(CAP, 1) == [rid]         # only its own worker resumes it
    assert await _account_of(rid) == "owner"


async def test_poll_shows_the_account_of_a_completed_and_a_failed_run():
    pid, _ = await _project()
    done = await _run(pid, "claude-2")
    await append_event(done, ev.RunCompleted(final_text="ok"))
    resp = proxy._job_response((await get_run(done, pid))[1])
    assert resp.status == "done" and resp.account_id == "claude-2"
    bad = await _run(pid, "owner")
    await append_event(bad, ev.RunFailed(error_class="cli_error", message="x"))
    assert proxy._job_response((await get_run(bad, pid))[1]).account_id == "owner"


async def test_poll_shows_the_account_while_the_run_is_pending():
    pid, _ = await _project()
    rid = await _run(pid, "claude-2")
    resp = proxy._job_response((await get_run(rid, pid))[1])
    assert resp.status == "pending" and resp.account_id == "claude-2"


async def test_attestation_records_the_account():
    pid, _ = await _project()
    rid = await _run(pid, "claude-2")
    await append_event(rid, ev.BillingAttested(
        auth_method="claude.ai", api_provider="firstParty", paid_continuation_verified_off=True,
        fallback="none", account_id="claude-2"))
    assert (await get_attestation(rid))["account_id"] == "claude-2"


# --- catalog ------------------------------------------------------------------------------------


def test_catalog_lists_opus_and_haiku_but_keeps_them_unverified(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS", "default")
    rows = {r["id"]: r for r in listing()}
    for model in ("claude-opus-5-5", "claude-haiku-5-5"):
        assert rows[f"claude-sub/{model}"]["available"] is False
        assert subscription_model_verified(f"claude-sub/{model}") is False
    assert rows["claude-sub/claude-sonnet-5-5"]["available"] is True
    assert not any("fable" in r_id for r_id in rows)
