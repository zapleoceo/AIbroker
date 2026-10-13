"""Multi-account Claude subscription runs: per-account worker claim, account selection for
/v1/jobs (policy order, quota pauses), same-account quota waits, account in the poll."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select

from aibroker.agent import events as ev
from aibroker.agent import policy as pol
from aibroker.agent import worker
from aibroker.agent.accounts import QUOTA_LOOKBACK, _reset_of, pick_account, quota_resets
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.policy import ProjectPolicy, policy_for
from aibroker.agent.runs import (
    append_event,
    create_run,
    get_attestation,
    get_run,
    list_events,
    park_for_quota,
)
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow, DeepJobRow, ProjectRow
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


# --- unclaimed runs / fail-closed account ------------------------------------------------------


async def _age(rid: int, seconds: float) -> None:
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).created_at = _now() - timedelta(seconds=seconds)


def _pending_max(monkeypatch, value: float) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(AGENT_PENDING_MAX_S=value))


async def test_unclaimed_run_for_an_unserved_account_fails_with_a_clear_error(monkeypatch):
    pid, _ = await _project()
    old, fresh = await _run(pid, "ghost", "a"), await _run(pid, "ghost", "b")
    await _age(old, 3600)
    _pending_max(monkeypatch, 1800)
    assert await worker.fail_unclaimed_runs() == 1
    row, job = await get_run(old, pid)
    assert row.outcome == "failed" and job.status == "error"
    assert job.error_message.startswith("account_unavailable: no worker claimed this run for account ghost")
    assert (await get_run(fresh, pid))[1].status == "pending"


async def test_reaper_skips_quota_parked_started_and_claimed_runs(monkeypatch):
    pid, _ = await _project()
    parked, started, claimed = [await _run(pid, "owner", t) for t in "abc"]
    await park_for_quota(parked, _now() + timedelta(days=1))
    await append_event(started, ev.SessionAssigned(session_id="S"))
    async with get_session() as s:
        (await s.get(DeepJobRow, claimed)).status = "running"
    for rid in (parked, started, claimed):
        await _age(rid, 7200)
    _pending_max(monkeypatch, 1800)
    assert await worker.fail_unclaimed_runs() == 0
    for rid in (parked, started, claimed):
        assert (await get_run(rid, pid))[0].outcome is None


async def test_run_queued_behind_a_running_run_of_the_account_is_not_reaped(monkeypatch):
    pid, _ = await _project()
    busy, queued = await _run(pid, "owner", "a"), await _run(pid, "owner", "b")
    async with get_session() as s:
        (await s.get(DeepJobRow, busy)).status = "running"
    await _age(queued, 7200)
    _pending_max(monkeypatch, 1800)
    assert await worker.fail_unclaimed_runs() == 0
    assert (await get_run(queued, pid))[1].status == "pending"


async def test_recent_start_on_the_account_means_a_live_worker(monkeypatch):
    pid, _ = await _project()
    done, queued = await _run(pid, "owner", "a"), await _run(pid, "owner", "b")
    async with get_session() as s:
        job = await s.get(DeepJobRow, done)
        job.status, job.started_at = "done", _now() - timedelta(seconds=60)
    await _age(queued, 7200)
    _pending_max(monkeypatch, 1800)
    assert await worker.fail_unclaimed_runs() == 0
    async with get_session() as s:      # an OLD start no longer counts
        (await s.get(DeepJobRow, done)).started_at = _now() - timedelta(seconds=7200)
    assert await worker.fail_unclaimed_runs() == 1


async def test_run_queued_behind_a_long_run_that_just_finished_is_not_reaped(monkeypatch):
    pid, _ = await _project()
    long_run, queued = await _run(pid, "owner", "a"), await _run(pid, "owner", "b")
    async with get_session() as s:       # started 2 h ago (wall timeout up to 7200 s), done now
        job = await s.get(DeepJobRow, long_run)
        job.status, job.started_at = "done", _now() - timedelta(hours=2)
        job.completed_at = _now() - timedelta(seconds=30)
    await _age(queued, 7200)
    _pending_max(monkeypatch, 1800)
    assert await worker.fail_unclaimed_runs() == 0
    assert (await get_run(queued, pid))[1].status == "pending"
    async with get_session() as s:       # only its last event is recent
        job = await s.get(DeepJobRow, long_run)
        job.completed_at = None
        (await s.get(AgentRunRow, long_run)).last_event_at = _now() - timedelta(seconds=30)
    assert await worker.fail_unclaimed_runs() == 0


async def test_a_run_claimed_between_the_scan_and_the_write_is_not_failed(monkeypatch):
    pid, _ = await _project()
    rid = await _run(pid, "ghost")
    await _age(rid, 7200)
    _pending_max(monkeypatch, 1800)
    real = worker.append_event

    async def claim_first(run_id, event, *a, **kw):
        async with get_session() as s:
            job = await s.get(DeepJobRow, run_id)
            job.status, job.started_at = "running", _now()
        return await real(run_id, event, *a, **kw)

    monkeypatch.setattr(worker, "append_event", claim_first)
    assert await worker.fail_unclaimed_runs() == 0
    row, job = await get_run(rid, pid)
    assert job.status == "running" and row.outcome is None
    assert [e.type for e in await list_events(rid)] == []


async def test_reaper_can_be_disabled(monkeypatch):
    pid, _ = await _project()
    rid = await _run(pid, "ghost")
    await _age(rid, 99999)
    _pending_max(monkeypatch, 0)
    assert await worker.fail_unclaimed_runs() == 0


def test_claude_worker_without_an_account_refuses_to_start(tmp_path):
    marker = tmp_path / "marker"
    marker.write_text("x")
    env = {"CLAUDE_CONFIG_DIR": "/x", "CODEX_HOME": "/y"}
    kw = {"env": env, "marker": str(marker)}
    assert "AGENT_ACCOUNT_ID" in worker.worker_image_problem(
        capabilities=("agent:claude",), account_id="  ", **kw)
    assert worker.worker_image_problem(capabilities=("agent:claude",), account_id="owner", **kw) is None
    assert worker.worker_image_problem(capabilities=("agent:codex",), account_id="", **kw) is None


async def test_worker_loop_refuses_to_start_for_claude_without_an_account(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        AGENT_WORKER_CAPABILITIES="agent:claude", AGENT_ACCOUNT_ID=""))
    with pytest.raises(RuntimeError, match="AGENT_ACCOUNT_ID"):
        await worker.agent_worker_loop(asyncio.Event())


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


async def test_account_stays_paused_until_the_latest_future_reset():
    pid, _ = await _project()
    rid = await _run(pid, "owner")
    later = _now() + timedelta(hours=5)
    await park_for_quota(rid, later)
    await park_for_quota(rid, _now() - timedelta(minutes=1))     # a wait that already ended
    paused = await quota_resets(["owner"], CAP)
    assert abs((paused["owner"] - later).total_seconds()) < 1


async def test_a_three_day_old_weekly_signal_with_its_reset_ahead_still_pauses():
    pid, _ = await _project()
    rid = await _run(pid, "owner")
    reset = datetime.now(UTC) + timedelta(days=2)
    await append_event(rid, ev.RunFailed(error_class="quota_exhausted",
                                         reset_at=reset.isoformat()))
    async with get_session() as s:
        for row in (await s.execute(select(AgentRunEventRow))).scalars():
            row.ts = _now() - timedelta(days=3)
    assert await pick_account(("owner", "claude-2"), CAP) == "claude-2"
    assert "owner" in await quota_resets(["owner"], CAP)


async def test_a_signal_older_than_the_lookback_is_ignored():
    pid, _ = await _project()
    rid = await _run(pid, "owner")
    await park_for_quota(rid, _now() + timedelta(days=1))
    async with get_session() as s:
        for row in (await s.execute(select(AgentRunEventRow))).scalars():
            row.ts = _now() - QUOTA_LOOKBACK - timedelta(hours=1)
    assert await quota_resets(["owner"], CAP) == {}


def test_reset_parsing_tolerates_garbage_and_timezones():
    assert _reset_of("run.waiting_quota", {}) is None                    # missing
    assert _reset_of("run.waiting_quota", {"not_before": 5}) is None     # not a string
    assert _reset_of("run.failed", {"error_class": "quota_exhausted", "reset_at": "soon"}) is None
    assert _reset_of("run.failed", {"error_class": "cli_error", "reset_at": "2030-01-01T00:00:00"}) is None
    assert _reset_of("run.failed", {"error_class": "quota_exhausted",
                                    "reset_at": "2030-01-01T02:00:00+02:00"}) == datetime(2030, 1, 1)


async def test_deadline_expiry_keeps_the_reset_so_the_account_stays_paused():
    pid, _ = await _project()
    rid = await _run(pid, "owner")
    not_before = _now() + timedelta(days=2)
    await park_for_quota(rid, not_before, reset_reported=True)
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        job.created_at = _now() - timedelta(days=1)
        job.request = {**job.request, "deadline_s": 60}
    assert await worker.expire_overdue_waiting() == 1
    last = (await list_events(rid))[-1]
    assert last.type == "run.failed" and last.payload["reset_at"]
    assert "owner" in await quota_resets(["owner"], CAP)


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
