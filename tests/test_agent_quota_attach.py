"""Agent runs: quota exhaustion WAITS (never fails over), and input attachments are real
files in the workspace. FakeAgentCLI only - no CLI, no network."""
from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select

from aibroker.agent import attachments as att
from aibroker.agent import events as ev
from aibroker.agent import real_cli, worker
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.parsers import parse_reset_at
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import _now, cancel_run, create_run, list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.main import app

_PID = [9800]
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


@pytest.fixture
def cfg(tmp_path):
    return ExecConfig(workspace_root=str(tmp_path / "ws"), cancel_poll_s=0.05, env={},
                      paid_continuation_verified_off=True)


async def _project():
    _PID[0] += 1
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"qa_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    return plain, _PID[0]


async def _run(pid=None, **kw) -> int:
    if pid is None:
        _, pid = await _project()
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:claude", prompt="p", task_id="t",
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], **kw)
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return rid


async def _row(rid):
    async with get_session() as s:
        return await s.get(AgentRunRow, rid), await s.get(DeepJobRow, rid)


def _quota(**kw):
    return ev.RunFailed(error_class="quota_exhausted", message="usage limit reached", **kw)


async def _again(rid):
    """Make a parked run claimable now and mark it running, like the worker would."""
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        job.run_after = _now() - timedelta(seconds=1)
        job.status = "running"


# --- quota = wait -------------------------------------------------------------------


async def test_quota_exhaustion_waits_with_backoff_and_is_visible(cfg):
    key, pid = await _project()
    rid = await _run(pid)
    fake = FakeAgentCLI([ev.SessionAssigned(session_id="S1"), _quota()])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, cfg) == "waiting_quota"
    run, job = await _row(rid)
    assert run.outcome is None and job.status == "pending" and run.quota_waits == 1
    wait = (job.run_after - _now()).total_seconds()
    assert 50 < wait <= 60
    types = [e.type for e in await list_events(rid)]
    assert types[-1] == "run.waiting_quota" and "run.failed" not in types
    assert run.resume_session_id == "S1"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.get(f"/v1/agent/runs/{rid}", headers={"X-Project-Key": key})).json()
    assert r["status"] == "waiting_quota" and r["not_before"] and r["quota_waits"] == 1


async def test_reported_reset_time_sets_not_before(cfg):
    rid = await _run()
    reset = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
    register_adapter("agent:claude", lambda: FakeAgentCLI([_quota(reset_at=reset)]))
    await execute_run(rid, cfg)
    _, job = await _row(rid)
    wait = (job.run_after - _now()).total_seconds()
    assert 600 < wait <= 606
    assert (await list_events(rid))[-1].payload["reset_reported"] is True


async def test_backoff_doubles_and_is_capped(cfg):
    waits = []
    for n in (0, 1, 2, 9):
        rid = await _run()
        async with get_session() as s:
            (await s.get(AgentRunRow, rid)).quota_waits = n
        register_adapter("agent:claude", lambda: FakeAgentCLI([_quota()]))
        await execute_run(rid, cfg)
        waits.append(round((await _row(rid))[1].run_after.timestamp() - _now().timestamp()))
    assert [w // 10 for w in waits] == [6, 12, 24, 180]       # 60, 120, 240, capped 1800


async def test_waiting_run_is_not_claimed_early_then_resumes_own_session(cfg, monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        AGENT_CONCURRENCY_CODEX=1, AGENT_CONCURRENCY_CLAUDE=1))
    rid = await _run(deadline_s=3600)
    register_adapter("agent:claude", lambda: FakeAgentCLI([
        ev.SessionAssigned(session_id="S1"), ev.Usage(input_tokens=4, turns=1), _quota()]))
    await execute_run(rid, cfg)
    assert await worker.claim_agent_runs("agent:claude", 1) == []          # still waiting
    await _again(rid)
    second = FakeAgentCLI([ev.Usage(input_tokens=6, turns=1), ev.RunCompleted(final_text="ok")])
    register_adapter("agent:claude", lambda: second)
    assert await execute_run(rid, cfg) == "completed"
    assert second.started.session_id == "S1" and second.started.account_id == "acc"
    run, _ = await _row(rid)
    assert (run.usage_input_tokens, run.usage_turns, run.quota_waits) == (10, 2, 1)


async def test_no_fallback_billing_guard_still_applies_on_retry(tmp_path):
    rid = await _run(deadline_s=3600)
    clean = ExecConfig(workspace_root=str(tmp_path / "w"), env={},
                      paid_continuation_verified_off=True)
    register_adapter("agent:claude", lambda: FakeAgentCLI([_quota()]))
    await execute_run(rid, clean)
    await _again(rid)
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    dirty = ExecConfig(workspace_root=str(tmp_path / "w"), env={"ANTHROPIC_API_KEY": "x"},
                      paid_continuation_verified_off=True)
    assert await execute_run(rid, dirty) == "failed" and fake.started is None
    async with get_session() as s:
        assert len((await s.execute(select(DeepJobRow))).scalars().all()) == 1


async def test_wait_beyond_overall_deadline_times_out_instead(cfg):
    rid = await _run(deadline_s=60)
    register_adapter("agent:claude", lambda: FakeAgentCLI([_quota(
        reset_at=(datetime.now(UTC) + timedelta(hours=2)).isoformat())]))
    assert await execute_run(rid, cfg) == "timed_out"
    last = (await list_events(rid))[-1]
    assert last.type == "run.timed_out" and last.payload["limit"] == "deadline"


async def test_waiting_run_past_deadline_is_expired_by_the_worker(cfg):
    rid = await _run(deadline_s=3600)
    register_adapter("agent:claude", lambda: FakeAgentCLI([_quota()]))
    await execute_run(rid, cfg)
    assert await worker.expire_overdue_waiting() == 0
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).created_at = _now() - timedelta(hours=2)
    assert await worker.expire_overdue_waiting() == 1
    run, job = await _row(rid)
    assert run.outcome == "timed_out" and job.status == "error"


async def test_execute_after_deadline_times_out_without_starting(cfg):
    rid = await _run(deadline_s=60)
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).created_at = _now() - timedelta(hours=1)
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, cfg) == "timed_out" and fake.started is None


async def test_cancel_while_waiting_for_quota(cfg):
    rid = await _run(deadline_s=3600)
    register_adapter("agent:claude", lambda: FakeAgentCLI([_quota()]))
    await execute_run(rid, cfg)
    run, _ = await _row(rid)
    res = await cancel_run(rid, run.project_id)
    assert res[0].outcome == "cancelled"


def test_parse_reset_at_understands_epoch_and_relative_wording():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_reset_at("Claude AI usage limit reached|1767225600").startswith("2026-01-01")
    assert parse_reset_at("try again in 30 minutes", now) == (now + timedelta(minutes=30)).isoformat()
    assert parse_reset_at("resets in 2 hours", now) == (now + timedelta(hours=2)).isoformat()
    assert parse_reset_at("boom") is None


# --- attachments --------------------------------------------------------------------


def _a(name="shot.png", mt="image/png", data=PNG):
    return {"name": name, "media_type": mt, "data_b64": base64.b64encode(data).decode()}


async def _submit(key, **extra):
    body = {"capability": "agent:claude", "prompt": "look", "task_id": "T", "account_id": "a"}
    body.update(extra)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/v1/agent/runs", json=body, headers={"X-Project-Key": key})


async def test_api_accepts_valid_image_and_rejects_bad_ones(monkeypatch):
    key, _ = await _project()
    assert (await _submit(key, attachments=[_a()])).status_code == 202
    bad = [
        [_a(mt="application/pdf")], [_a(name="x.jpg")], [_a(data=b"not an image")],
        [_a(name="../x.png")], [_a(), _a()], [_a(name=f"f{i}.png") for i in range(6)],
        [{"name": "a.png", "media_type": "image/png", "data_b64": "!!!"}],
    ]
    for atts in bad:
        assert (await _submit(key, attachments=atts)).status_code == 422, atts
    monkeypatch.setattr(att, "MAX_BYTES", 16)
    assert (await _submit(key, attachments=[_a(name="big.png")], task_id="T2")).status_code == 422


async def test_total_size_cap(monkeypatch):
    monkeypatch.setattr(att, "MAX_TOTAL_BYTES", 50)
    with pytest.raises(ValueError, match="in total"):
        att.validate_attachments([_a(name="a.png"), _a(name="b.png")])


async def test_real_bytes_land_in_workspace_not_a_description(cfg):
    rid = await _run(attachments=[_a(), _a(name="two.png")])

    class Spy(FakeAgentCLI):
        seen = {}

        async def start(self, run):
            for rel in run.attachments:
                p = os.path.join(run.workspace, rel)
                Spy.seen[rel] = (open(p, "rb").read(), os.stat(p).st_mode & 0o777)  # noqa: SIM115
            async for e in super().start(run):
                yield e

    spy = Spy([ev.RunCompleted(final_text="seen")])
    register_adapter("agent:claude", lambda: spy)
    assert await execute_run(rid, cfg) == "completed"
    assert sorted(Spy.seen) == ["inputs/shot.png", "inputs/two.png"]
    assert all(v[0] == PNG for v in Spy.seen.values())
    if os.name == "posix":
        assert all(v[1] == 0o600 for v in Spy.seen.values())
    assert spy.started.attachments == ["inputs/shot.png", "inputs/two.png"]
    _, job = await _row(rid)
    assert all(a["data_b64"] is None for a in job.request["attachments"])   # bytes dropped when done
    assert job.result_meta["files"] == []                                     # inputs are not outputs


async def test_corrupt_attachment_fails_run_before_start(cfg):
    rid = await _run(attachments=[_a(data=b"GIF89a-but-png-declared")])
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    assert await execute_run(rid, cfg) == "failed" and fake.started is None
    assert (await list_events(rid))[-1].payload["error_class"] == "attachment_invalid"


def test_claude_argv_and_prompt_reference_the_real_files():
    spec = AgentRunSpec(run_id=1, capability="agent:claude", prompt="grade this",
                        workspace="/w", account_id="a", attachments=["inputs/shot.png"])
    argv = real_cli.build_claude_argv(spec)
    assert argv[-2:] == ["--allowedTools", "Read"]
    prompt = real_cli.build_claude_prompt(spec)
    assert "inputs/shot.png" in prompt and prompt.startswith("grade this")
    explicit = AgentRunSpec(run_id=1, capability="agent:claude", prompt="p", workspace="/w",
                            account_id="a", attachments=["inputs/a.png"],
                            allowed_tools=["Read", "Edit"])
    assert real_cli.build_claude_argv(explicit)[-3:] == ["--allowedTools", "Read", "Edit"]
    plain = AgentRunSpec(run_id=1, capability="agent:claude", prompt="p", workspace="/w",
                         account_id="a")
    assert "--allowedTools" not in real_cli.build_claude_argv(plain)
    assert real_cli.build_claude_prompt(plain) == "p"
