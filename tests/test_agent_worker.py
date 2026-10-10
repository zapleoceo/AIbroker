"""Agent runs C2: executor (timeouts, cancel, workspace, files, model, usage), session lock,
dedicated worker lane. Everything runs against FakeAgentCLI - no CLI, no network."""
from __future__ import annotations

import asyncio
import base64
import os
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from aibroker.agent import events as ev
from aibroker.agent import worker
from aibroker.agent.adapter import ModelInfo
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.lock import session_lock
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import append_event, cancel_run, create_run, get_run, list_events
from aibroker.agent.workspace import collect_files, create_workspace
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow

_PID = [9000]


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


@pytest.fixture
def cfg(tmp_path):
    return ExecConfig(workspace_root=str(tmp_path / "ws"), cancel_poll_s=0.05,
                      cancel_grace_s=1.0, file_max_bytes=100, files_total_bytes=150,
                      files_max_count=3, files_ttl_s=3600, env={},
                      paid_continuation_verified_off=True)


async def _project() -> int:
    _PID[0] += 1
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"aw_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    return _PID[0]


async def _run(pid=None, *, cap="agent:codex", wall=30, idle=30, model=None, session_id=None,
               task="T", account="acc", **kw) -> int:
    pid = pid or await _project()
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=cap, prompt="p", task_id=task,
        account_id=account, session_id=session_id, idempotency_key=None,
        wall_timeout_s=wall, idle_timeout_s=idle, allowed_tools=[], model=model, **kw)
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        job.status = "running"
    return rid


async def _types(rid):
    return [e.type for e in await list_events(rid)]


async def _row(rid):
    async with get_session() as s:
        return await s.get(AgentRunRow, rid), await s.get(DeepJobRow, rid)


def _use(fake, cap="agent:codex"):
    register_adapter(cap, lambda: fake)
    return fake


OK = [ev.SessionAssigned(session_id="S1"), ev.MessageDelta(text="hi"),
      ev.Usage(input_tokens=10, output_tokens=5, turns=1),
      ev.Usage(input_tokens=2, output_tokens=1, turns=1),
      ev.RunCompleted(final_text="done")]


# --- happy path, usage, billing separation -----------------------------------


async def test_run_completes_with_ordered_events_and_usage_without_cost(cfg):
    rid = await _run()
    _use(FakeAgentCLI(list(OK)))
    assert await execute_run(rid, cfg) == "completed"
    assert await _types(rid) == ["run.started", "session.assigned", "message.delta",
                                 "usage", "usage", "run.completed"]
    run, job = await _row(rid)
    assert (run.session_id, job.status, job.result_text) == ("S1", "done", "done")
    assert (run.usage_input_tokens, run.usage_output_tokens, run.usage_turns) == (12, 6, 2)
    assert run.usage_source == "cli_reported" and run.cost_usd is None


async def test_run_without_usage_has_null_usage_source(cfg):
    rid = await _run()
    _use(FakeAgentCLI([ev.RunCompleted(final_text="x")]))
    await execute_run(rid, cfg)
    run, _ = await _row(rid)
    assert run.usage_source is None and run.cost_usd is None


async def test_usage_is_not_written_to_the_api_usage_log(cfg):
    from aibroker.db.models import UsageLogRow
    rid = await _run()
    _use(FakeAgentCLI(list(OK)))
    await execute_run(rid, cfg)
    async with get_session() as s:
        assert (await s.execute(select(UsageLogRow))).first() is None


# --- failure modes ------------------------------------------------------------


async def test_no_adapter_registered_fails_adapter_unavailable(cfg):
    rid = await _run()
    assert await execute_run(rid, cfg) == "failed"
    _, job = await _row(rid)
    assert job.error_message.startswith("adapter_unavailable")


async def test_adapter_crash_and_missing_terminal_are_failed_runs(cfg):
    a, b = await _run(task="a"), await _run(task="b")
    _use(FakeAgentCLI([ev.MessageDelta(text="x"), ("raise", "kaput")]))
    assert await execute_run(a, cfg) == "failed"
    _use(FakeAgentCLI([ev.MessageDelta(text="x")]))
    assert await execute_run(b, cfg) == "failed"
    assert (await _row(a))[1].error_message.startswith("adapter_error: kaput")
    assert (await _row(b))[1].error_message.startswith("no_terminal_event")


# --- timeouts and cancel -------------------------------------------------------


async def test_wall_timeout_cancels_adapter_and_ends_timed_out(cfg):
    rid = await _run(wall=0.3, idle=30)
    fake = _use(FakeAgentCLI([ev.MessageDelta(text="a"), ("hang",)]))
    assert await execute_run(rid, cfg) == "timed_out"
    assert fake.cancel_calls == 1
    last = (await list_events(rid))[-1]
    assert last.type == "run.timed_out" and last.payload["limit"] == "wall"


async def test_idle_timeout_when_no_events_arrive(cfg):
    rid = await _run(wall=30, idle=0.3)
    fake = _use(FakeAgentCLI([("hang",)]))
    assert await execute_run(rid, cfg) == "timed_out"
    assert (await list_events(rid))[-1].payload["limit"] == "idle" and fake.cancel_calls == 1


async def test_events_keep_idle_timer_alive(cfg):
    rid = await _run(wall=30, idle=0.4)
    script = []
    for _ in range(4):
        script += [("sleep", 0.2), ev.MessageDelta(text="tick")]
    _use(FakeAgentCLI(script + [ev.RunCompleted(final_text="ok")]))
    assert await execute_run(rid, cfg) == "completed"


async def test_cancel_flag_stops_a_running_run(cfg):
    rid = await _run()
    fake = _use(FakeAgentCLI([ev.MessageDelta(text="a"), ("hang",)]))
    task = asyncio.create_task(execute_run(rid, cfg))
    await asyncio.sleep(0.2)
    res = await cancel_run(rid, (await _row(rid))[0].project_id)
    assert res[0].cancel_requested is True
    assert await asyncio.wait_for(task, 5) == "cancelled"
    assert fake.cancel_calls == 1 and (await _types(rid))[-1] == "run.cancelled"
    assert (await _row(rid))[1].status == "error"


async def test_cancel_before_start_never_starts_the_adapter(cfg):
    rid = await _run()
    fake = _use(FakeAgentCLI(list(OK)))
    async with get_session() as s:
        (await s.get(AgentRunRow, rid)).cancel_requested = True
    assert await execute_run(rid, cfg) == "cancelled"
    assert fake.started is None


async def test_finished_run_is_not_executed_twice(cfg):
    rid = await _run()
    _use(FakeAgentCLI(list(OK)))
    await execute_run(rid, cfg)
    fake2 = _use(FakeAgentCLI(list(OK)))
    assert await execute_run(rid, cfg) == "completed" and fake2.started is None


# --- workspace -----------------------------------------------------------------


async def test_workspace_is_per_run_private_and_removed_afterwards(cfg):
    seen = []

    class Spy(FakeAgentCLI):
        async def start(self, run):
            seen.append(run.workspace)
            assert os.path.isdir(run.workspace)
            if os.name == "posix":
                assert oct(os.stat(run.workspace).st_mode & 0o777) == "0o700"
            async for e in super().start(run):
                yield e

    a, b = await _run(task="a"), await _run(task="b")
    register_adapter("agent:codex", lambda: Spy([("write", "f.txt", "1"),
                                                 ev.RunCompleted(final_text="")]))
    await execute_run(a, cfg)
    await execute_run(b, cfg)
    assert len(set(seen)) == 2 and all(f"run-{r}" in p for r, p in zip((a, b), seen, strict=True))
    assert not any(os.path.exists(p) for p in seen)


async def test_workspace_removed_even_when_run_fails(cfg):
    rid = await _run()
    fake = _use(FakeAgentCLI([("write", "x", "1"), ("raise", "boom")]))
    await execute_run(rid, cfg)
    assert not os.path.exists(fake.started.workspace)


# --- files ---------------------------------------------------------------------


async def test_file_changed_files_are_returned_with_content(cfg):
    rid = await _run()
    _use(FakeAgentCLI([("write", "out/a.txt", "hello"),
                       ev.FileChanged(path="out/a.txt", action="created"),
                       ev.RunCompleted(final_text="ok")]))
    await execute_run(rid, cfg)
    (f,) = (await _row(rid))[1].result_meta["files"]
    assert f["path"] == "out/a.txt" and base64.b64decode(f["content_b64"]) == b"hello"
    assert f["expires_at"] and f["skipped"] is None


def test_collect_files_caps_unsafe_paths_and_symlinks(tmp_path):
    ws = create_workspace(str(tmp_path / "root"), 1)
    (tmp_path / "secret.txt").write_text("top secret")
    Path(ws, "big.bin").write_bytes(b"x" * 101)
    for n, size in (("a", 60), ("b", 60), ("c", 10), ("d", 1)):
        Path(ws, n).write_bytes(b"y" * size)
    try:
        os.symlink(tmp_path / "secret.txt", os.path.join(ws, "link.txt"))
        has_link = True
    except (OSError, NotImplementedError):
        has_link = False
    paths = ["big.bin", "a", "b", "c", "d", "../secret.txt", "/etc/passwd", "nope"]
    if has_link:
        paths.append("link.txt")
    got = {f.path: f for f in collect_files(ws, paths, max_bytes=100, total_bytes=150,
                                            max_count=5, ttl_s=60)}
    assert got["big.bin"].skipped == "too_large" and got["big.bin"].content_b64 is None
    assert got["a"].content_b64 and got["b"].content_b64
    assert got["c"].skipped == "cap_reached" or got["c"].content_b64   # total 150: 60+60+10
    assert got["../secret.txt"].skipped == "unsafe_path_or_missing"
    assert got["/etc/passwd"].skipped == "unsafe_path_or_missing"
    assert got["nope"].skipped == "unsafe_path_or_missing"
    if has_link:
        assert got["link.txt"].skipped == "unsafe_path_or_missing"
    assert sum(1 for f in got.values() if f.content_b64) <= 5


def test_collect_files_count_cap(tmp_path):
    ws = create_workspace(str(tmp_path), 2)
    for i in range(4):
        Path(ws, f"f{i}").write_text("x")
    out = collect_files(ws, [f"f{i}" for i in range(4)], max_bytes=10, total_bytes=100,
                        max_count=2, ttl_s=60)
    assert [f.skipped for f in out] == [None, None, "cap_reached", "cap_reached"]


async def test_expired_file_content_is_not_served(cfg):
    from httpx import ASGITransport, AsyncClient

    from aibroker.main import app
    pid = await _project()
    plain = generate_project_key()
    async with get_session() as s:
        (await s.get(ProjectRow, pid)).project_key_hash = hash_project_key(plain)
    rid = await _run(pid)
    await append_event(rid, ev.RunCompleted(final_text="x", files=[
        ev.FileRef(path="a", size=1, content_b64="eA==", expires_at="2000-01-01T00:00:00+00:00")]))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/v1/agent/runs/{rid}", headers={"X-Project-Key": plain})
    f = r.json()["files"][0]
    assert f["expired"] is True and f["content_b64"] is None and f["path"] == "a"


# --- model: exact id, never substituted -----------------------------------------


async def test_explicit_model_reaches_adapter_unchanged(cfg):
    rid = await _run(model="gpt-5.1-codex")
    fake = _use(FakeAgentCLI([ev.RunCompleted()], models=[ModelInfo("gpt-5.1-codex")]))
    assert await execute_run(rid, cfg) == "completed"
    assert fake.started.model == "gpt-5.1-codex"
    assert (await _row(rid))[0].model == "gpt-5.1-codex"


async def test_model_missing_from_cli_catalog_fails_without_starting(cfg):
    rid = await _run(model="claude-opus-9")
    fake = _use(FakeAgentCLI([ev.RunCompleted()], models=[ModelInfo("claude-sonnet-5")]))
    assert await execute_run(rid, cfg) == "failed"
    assert fake.started is None
    assert (await _row(rid))[1].error_message.startswith("model_unavailable")


async def test_catalog_unavailable_passes_exact_id_and_latest_only_if_asked(cfg):
    a, b, c = await _run(model="m-1", task="a"), await _run(model="latest", task="b"), \
        await _run(task="c")
    for rid, want in ((a, "m-1"), (b, "latest"), (c, None)):
        fake = _use(FakeAgentCLI([ev.RunCompleted()], models=None))
        await execute_run(rid, cfg)
        assert fake.started.model == want


# --- resume + session lock ------------------------------------------------------


async def test_resume_passes_exact_session_id_to_adapter(cfg):
    pid = await _project()
    first = await _run(pid, account="acc")
    _use(FakeAgentCLI([ev.SessionAssigned(session_id="S9"), ev.RunCompleted()]))
    await execute_run(first, cfg)
    second = await _run(pid, account="acc", session_id="S9", task="T2")
    fake = _use(FakeAgentCLI([ev.RunCompleted()]))
    await execute_run(second, cfg)
    assert fake.started.session_id == "S9"
    assert (await list_events(second))[0].payload["resumed_session_id"] == "S9"


async def test_session_lock_is_exclusive_per_account_and_session():
    async with session_lock("a", "s") as one:
        assert one is True
        async with session_lock("a", "s") as two:
            assert two is False
        async with session_lock("a", "other") as three, session_lock("b", "s") as four:
            assert three is True and four is True
    async with session_lock("a", "s") as again:
        assert again is True


async def test_resume_run_is_requeued_not_failed_while_session_is_locked(cfg):
    pid = await _project()
    first = await _run(pid)
    _use(FakeAgentCLI([ev.SessionAssigned(session_id="SL"), ev.RunCompleted()]))
    await execute_run(first, cfg)
    second = await _run(pid, session_id="SL", task="T2")
    fake = _use(FakeAgentCLI([ev.RunCompleted()]))
    async with session_lock("acc", "SL"):
        assert await execute_run(second, cfg) is None
    run, job = await _row(second)
    assert job.status == "pending" and job.retry_count == 1 and job.run_after is not None
    assert run.outcome is None and fake.started is None


async def test_session_lock_holder_blocks_second_run_end_to_end(cfg):
    """Two real runs on one session: the second never overlaps the first."""
    pid = await _project()
    first = await _run(pid)
    _use(FakeAgentCLI([ev.SessionAssigned(session_id="SE"), ev.RunCompleted()]))
    await execute_run(first, cfg)
    r1 = await _run(pid, session_id="SE", task="T2")
    gate = FakeAgentCLI([("hang",)])
    _use(gate)
    t1 = asyncio.create_task(execute_run(r1, cfg))
    await asyncio.sleep(0.2)
    r2 = await _run(pid, session_id="SE", task="T3")
    assert await execute_run(r2, cfg) is None          # blocked while r1 holds the session
    await gate.cancel()
    await asyncio.wait_for(t1, 5)


# --- worker lane -----------------------------------------------------------------


def _settings(**over):
    base = {"AGENT_CONCURRENCY_CODEX": 1, "AGENT_CONCURRENCY_CLAUDE": 1}
    base.update(over)
    return SimpleNamespace(**base)


async def _pending(pid, cap, task):
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability=cap, prompt="p", task_id=task,
        account_id="acc", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    return rid


async def test_claim_respects_per_capability_concurrency_and_lane(monkeypatch):
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    pid = await _project()
    codex = [await _pending(pid, "agent:codex", f"t{i}") for i in range(3)]
    claude = await _pending(pid, "agent:claude", "c")
    chat_id = 777
    async with get_session() as s:
        s.add(DeepJobRow(id=chat_id, project_id=pid, capability="chat:fast", status="pending",
                         request={"messages": []}))
    assert worker.concurrency_for("agent:codex") == 1
    assert await worker.claim_agent_runs("agent:codex", worker.concurrency_for("agent:codex")) \
        == [codex[0]]
    assert await worker.claim_agent_runs("agent:claude", 5) == [claude]
    async with get_session() as s:
        assert (await s.get(DeepJobRow, chat_id)).status == "pending"   # LLM job untouched
        assert (await s.get(DeepJobRow, codex[1])).status == "pending"
    assert await worker.claim_agent_runs("agent:codex", 0) == []


async def test_agent_slots_are_independent_of_llm_concurrency(monkeypatch):
    from aibroker.services import job_queue
    monkeypatch.setattr(worker, "get_settings", lambda: _settings(AGENT_CONCURRENCY_CODEX=2))
    assert worker.concurrency_for("agent:codex") == 2
    assert worker.concurrency_for("agent:codex") != job_queue._MAX_CONCURRENCY


async def test_claim_skips_backoff_and_drain_runs_to_completion(monkeypatch, cfg):
    monkeypatch.setattr(worker, "get_settings", lambda: _settings())
    pid = await _project()
    waiting = await _pending(pid, "agent:codex", "w")
    ready = await _pending(pid, "agent:claude", "r")
    async with get_session() as s:
        from aibroker.agent.runs import _now
        (await s.get(DeepJobRow, waiting)).run_after = _now() + timedelta(seconds=60)
    register_adapter("agent:codex", lambda: FakeAgentCLI([ev.RunCompleted(final_text="a")]))
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted(final_text="b")]))
    assert await worker.drain_agent_once(cfg) == 1
    assert (await get_run(ready, (await _row(ready))[0].project_id))[1].result_text == "b"
    assert (await _row(waiting))[1].status == "pending"


async def test_stale_running_agent_run_is_failed_not_requeued(monkeypatch):
    pid = await _project()
    rid = await _pending(pid, "agent:codex", "s")
    from aibroker.agent.runs import _now
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        job.status = "running"
        job.started_at = _now() - timedelta(seconds=30 + worker.STALE_GRACE_S + 5)
    assert await worker.fail_stale_agent_runs() == 1
    run, job = await _row(rid)
    assert run.outcome == "failed" and job.error_message.startswith("worker_lost")
    assert await worker.fail_stale_agent_runs() == 0


async def test_fresh_running_agent_run_is_left_alone():
    pid = await _project()
    rid = await _pending(pid, "agent:codex", "f")
    from aibroker.agent.runs import _now
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        job.status = "running"
        job.started_at = _now()
    assert await worker.fail_stale_agent_runs() == 0


@pytest.mark.skipif("sqlite" in os.environ.get("DATABASE_URL", ""),
                    reason="FOR UPDATE SKIP LOCKED needs Postgres")
async def test_concurrent_claims_never_double_claim_a_run():
    pid = await _project()
    rid = await _pending(pid, "agent:codex", "one")
    got = await asyncio.gather(*(worker.claim_agent_runs("agent:codex", 1) for _ in range(4)))
    assert sorted(i for ids in got for i in ids) == [rid]
