"""Session lease + fencing (agent_session_leases): the agent worker needs only the pgbouncer
connection. Atomic acquire, expiry takeover, worker restart, heartbeat, release, fenced writes,
and transaction-pool compatibility (no session-level state)."""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import asyncpg
import pytest
from sqlalchemy import insert, select, text

from aibroker.agent import events as ev
from aibroker.agent import worker
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, exec_config_from_settings, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.lock import (
    Lease,
    StaleLease,
    acquire_lease,
    check_fence,
    release_lease,
    renew_lease,
    session_lock,
)
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import append_event, create_run, park_for_quota, record_usage
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import Settings
from aibroker.db import get_session
from aibroker.db.models import AgentSessionLeaseRow, DeepJobRow, ProjectRow

ROOT = Path(__file__).resolve().parent.parent
ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


async def _row(account="a", session="s"):
    async with get_session() as s:
        return (await s.execute(select(AgentSessionLeaseRow).where(
            AgentSessionLeaseRow.account_id == account,
            AgentSessionLeaseRow.session_id == session))).scalar_one_or_none()


async def _holder(account="a", session="s"):
    row = await _row(account, session)
    return row.run_id if row else None


# --- concurrent claim -----------------------------------------------------------------------


async def test_concurrent_acquire_on_one_session_has_exactly_one_winner():
    results = await asyncio.gather(*(acquire_lease("a", "s", run_id=i, ttl_s=60)
                                     for i in range(1, 13)))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1
    assert await _holder() == winners[0].run_id and winners[0].fence == 1


async def test_different_sessions_and_accounts_do_not_collide():
    assert await acquire_lease("a", "s1", 1) and await acquire_lease("a", "s2", 2)
    assert await acquire_lease("b", "s1", 3)
    assert await acquire_lease("a", "s1", 4) is None


# --- expiry, takeover, worker restart -------------------------------------------------------------


async def test_expired_lease_is_taken_over_with_a_larger_fence():
    old = await acquire_lease("a", "s", 1, ttl_s=-5)             # a dead worker's expired lease
    new = await acquire_lease("a", "s", 2, ttl_s=60)
    assert new is not None and new.fence > old.fence and await _holder() == 2
    assert not await renew_lease(old)                            # the old holder cannot revive it
    assert await acquire_lease("a", "s", 3) is None              # run 2's lease is live


async def test_worker_restart_stale_lease_is_taken_only_after_expiry():
    dead = await acquire_lease("a", "s", 1, ttl_s=0.4)           # the worker dies holding it
    assert dead is not None
    assert await acquire_lease("a", "s", 2, ttl_s=60) is None    # restarted worker: still live
    await asyncio.sleep(0.6)
    fresh = await acquire_lease("a", "s", 2, ttl_s=60)           # TTL elapsed -> takeover
    assert fresh is not None and fresh.fence == dead.fence + 1
    async with session_lock("a", "other", 2) as lease:           # and the new holder works
        assert lease is not None


async def test_live_lease_is_never_taken():
    assert await acquire_lease("a", "s", 1, ttl_s=60)
    assert await acquire_lease("a", "s", 2) is None
    assert await _holder() == 1


# --- renewal ----------------------------------------------------------------------------------------


async def test_heartbeat_extends_the_lease_past_its_ttl():
    async with session_lock("a", "s", 1, ttl_s=0.6, heartbeat_s=0.1) as lease:
        assert lease is not None
        await asyncio.sleep(1.0)                                 # > ttl: only the heartbeat holds it
        assert await acquire_lease("a", "s", 2, ttl_s=60) is None
        assert await _holder() == 1


async def test_missed_heartbeats_let_the_lease_expire():
    lease = await acquire_lease("a", "s", 1, ttl_s=0.2)          # nobody renews it
    assert lease is not None
    await asyncio.sleep(0.4)
    assert await acquire_lease("a", "s", 2, ttl_s=60) is not None


async def test_renew_extends_lease_until_and_needs_the_current_fence():
    lease = await acquire_lease("a", "s", 1, ttl_s=1)
    before = (await _row()).lease_until
    assert await renew_lease(lease, ttl_s=100)
    assert (await _row()).lease_until > before
    assert not await renew_lease(Lease("a", "s", 1, lease.fence + 5))   # wrong fence


# --- release --------------------------------------------------------------------------------------------


async def test_release_frees_the_session_but_keeps_the_fence_counter():
    async with session_lock("a", "s", 1) as lease:
        assert lease is not None
        other = Lease("a", "s", 99, lease.fence)
        await release_lease(other)                               # not the holder: no effect
        assert await acquire_lease("a", "s", 5) is None
    again = await acquire_lease("a", "s", 2)
    assert again is not None and again.fence == lease.fence + 1  # monotonic across releases


async def test_lock_context_yields_none_and_leaves_the_holder_alone():
    async with session_lock("a", "s", 1) as one:
        async with session_lock("a", "s", 2) as two:
            assert one is not None and two is None
        assert await _holder() == 1                              # the loser did not release it


async def test_lease_is_released_when_the_block_raises():
    with pytest.raises(RuntimeError):
        async with session_lock("a", "s", 1):
            raise RuntimeError("run crashed")
    assert await acquire_lease("a", "s", 2) is not None


# --- fencing ----------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


async def _run(pid, *, session_id=None, task="t"):
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:claude", prompt=task, task_id=task,
        account_id="acc", session_id=session_id, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    return rid


async def _project(pid=9990):
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid, name=f"lease_p{pid}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    return pid


async def _events(rid):
    from aibroker.agent.runs import list_events
    return [e.type for e in await list_events(rid)]


async def test_fence_tokens_are_monotonic_per_acquisition():
    fences = []
    for run_id in range(1, 6):
        lease = await acquire_lease("a", "s", run_id, ttl_s=60)
        fences.append(lease.fence)
        await release_lease(lease)
    assert fences == sorted(set(fences)) and fences[0] == 1 and len(fences) == 5


async def test_stale_holder_cannot_write_events_status_usage_or_park():
    pid = await _project()
    rid = await _run(pid)
    stale = await acquire_lease("acc", "S", rid, ttl_s=-5)       # zombie: lease expired...
    fresh = await acquire_lease("acc", "S", 7777, ttl_s=60)      # ...and taken over
    assert fresh.fence > stale.fence
    for coro in (
        append_event(rid, ev.MessageDelta(text="zombie"), stale),
        append_event(rid, ev.RunCompleted(final_text="zombie result"), stale),
        record_usage(rid, {"input_tokens": 5}, "subscription", stale),
        park_for_quota(rid, datetime(2999, 1, 1), lease=stale),
    ):
        with pytest.raises(StaleLease):
            await coro
    assert await _events(rid) == []                              # nothing was written
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
    assert job.status == "pending" and job.result_text is None


async def test_current_holder_writes_pass_and_get_recorded():
    pid = await _project()
    rid = await _run(pid)
    lease = await acquire_lease("acc", "S", rid, ttl_s=60)
    await append_event(rid, ev.MessageDelta(text="ok"), lease)
    await append_event(rid, ev.RunCompleted(final_text="done"), lease)
    assert await _events(rid) == ["message.delta", "run.completed"]


async def test_runs_without_a_lease_are_unfenced():
    pid = await _project()
    rid = await _run(pid)
    await append_event(rid, ev.MessageDelta(text="plain"))        # lease=None: nothing to check
    async with get_session() as s:
        await check_fence(s, None)


async def test_fence_rejects_a_forged_token_and_a_foreign_run():
    lease = await acquire_lease("a", "s", 1, ttl_s=60)
    for forged in (Lease("a", "s", 1, lease.fence + 1), Lease("a", "s", 2, lease.fence),
                   Lease("a", "nope", 1, lease.fence)):
        async with get_session() as s:
            with pytest.raises(StaleLease):
                await check_fence(s, forged)


async def test_zombie_run_stops_writing_after_takeover(tmp_path):
    """End to end: run A holds the session, its lease is taken over mid-run; A's next write is
    rejected, A stops, and nothing of A lands after the takeover."""
    pid = await _project()
    first = await _run(pid, task="first")
    await append_event(first, ev.SessionAssigned(session_id="SL"))
    await append_event(first, ev.RunCompleted())
    rid = await _run(pid, session_id="SL", task="second")
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"

    class Takeover(FakeAgentCLI):
        async def start(self, run):
            yield ev.MessageDelta(text="before")
            await asyncio.sleep(0.4)                             # let the executor store it first
            async with get_session() as s:                       # lease expires, a newer run wins it
                row = (await s.execute(select(AgentSessionLeaseRow))).scalar_one()
                row.lease_until = row.lease_until.replace(year=2000)
            assert await acquire_lease("acc", "SL", 8888, ttl_s=60) is not None
            yield ev.MessageDelta(text="after takeover")
            yield ev.RunCompleted(final_text="zombie result")

    register_adapter("agent:claude", lambda: Takeover([]))
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True,
                     cancel_poll_s=0.05)
    assert await execute_run(rid, cfg) is None                   # stopped, no terminal written
    types = await _events(rid)
    assert "message.delta" in types and "run.completed" not in types
    assert types.count("message.delta") == 1                     # only the pre-takeover delta
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
    assert job.result_text is None and job.status == "running"
    assert await _holder("acc", "SL") == 8888                    # the new holder is untouched


# --- executor integration -----------------------------------------------------------------------------


async def _resume_run(session_id="SL"):
    pid = await _project(9991)
    first = await _run(pid, task="first")
    await append_event(first, ev.SessionAssigned(session_id=session_id))
    await append_event(first, ev.RunCompleted())
    second = await _run(pid, session_id=session_id, task="second")
    async with get_session() as s:
        (await s.get(DeepJobRow, second)).status = "running"
    return second


async def test_a_resumed_run_holds_the_lease_while_running_and_frees_it_after(tmp_path):
    rid = await _resume_run()
    seen = {}

    class Spy(FakeAgentCLI):
        async def start(self, run):
            seen["holder"] = await _holder("acc", "SL")
            async for e in super().start(run):
                yield e

    register_adapter("agent:claude", lambda: Spy([ev.RunCompleted()]))
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"
    assert seen["holder"] == rid
    assert await acquire_lease("acc", "SL", 1) is not None       # free again


async def test_a_resumed_run_is_requeued_while_a_live_lease_exists(tmp_path):
    rid = await _resume_run()
    fake = FakeAgentCLI([ev.RunCompleted()])
    register_adapter("agent:claude", lambda: fake)
    assert await acquire_lease("acc", "SL", 4242, ttl_s=60)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) is None and fake.started is None
    assert await _holder("acc", "SL") == 4242


async def test_a_crashed_holders_lease_expires_and_the_run_proceeds(tmp_path):
    rid = await _resume_run()
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted()]))
    assert await acquire_lease("acc", "SL", 4242, ttl_s=-1)       # dead worker, TTL elapsed
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"


def test_lease_timings_come_from_settings_and_ttl_covers_three_heartbeats():
    cfg = exec_config_from_settings()
    assert (cfg.lease_ttl_s, cfg.lease_heartbeat_s) == (120.0, 30.0)
    assert cfg.lease_ttl_s >= 3 * cfg.lease_heartbeat_s


# --- pgbouncer only / transaction-pool compatible ------------------------------------------------------


async def test_the_worker_path_never_uses_the_direct_url_or_a_foreign_connection(monkeypatch):
    def boom(self):
        raise AssertionError("agent worker asked for the direct database URL")

    monkeypatch.setattr(Settings, "direct_database_url", property(boom))
    real_connect = asyncpg.connect

    async def spy(*a, **k):
        caller = inspect.stack()[1].filename.replace("\\", "/")
        assert "sqlalchemy" in caller, f"direct asyncpg.connect from {caller}"
        return await real_connect(*a, **k)

    monkeypatch.setattr(asyncpg, "connect", spy)
    monkeypatch.setattr(worker, "get_settings", lambda: SimpleNamespace(
        AGENT_CONCURRENCY_CODEX=1, AGENT_CONCURRENCY_CLAUDE=1, AGENT_ACCOUNT_ID=""))
    async with session_lock("a", "s", 1) as lease:
        assert lease is not None
    assert await worker.claim_agent_runs("agent:claude", 1) == []
    assert await worker.fail_stale_agent_runs() == 0
    assert await worker.expire_overdue_waiting() == 0


def test_agent_package_has_no_direct_connection_listen_or_session_state_code():
    for path in (ROOT / "src" / "aibroker" / "agent").glob("*.py"):
        code = "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                         if not ln.lstrip().startswith(("#", '"""')))
        for banned in ("import asyncpg", "direct_database_url", "pg_advisory", "add_listener",
                       "LISTEN ", "_listen_dsn", "pg_try_advisory", "PREPARE ", "SET SESSION",
                       "SET LOCAL", "pg_notify"):
            assert banned not in code, f"{path.name}: {banned}"


@pytest.mark.skipif(ON_SQLITE, reason="needs a real Postgres")
async def test_lease_and_fenced_writes_work_with_prepared_statements_off():
    """Transaction pooling hands every transaction a possibly different backend (the test engine is
    NullPool: a new connection per session) and some setups run with statement_cache_size=0.
    Nothing here may depend on session state or on prepared statements."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    import aibroker.db.engine as engine_mod
    pid = await _project()
    rid = await _run(pid)
    engine = create_async_engine(os.environ["DATABASE_URL"], poolclass=NullPool,
                                 connect_args={"statement_cache_size": 0,
                                               "prepared_statement_cache_size": 0})
    saved = engine_mod._sessionmaker
    engine_mod._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        lease = await acquire_lease("acc", "S", rid, ttl_s=60)
        assert lease is not None and await renew_lease(lease)
        await append_event(rid, ev.MessageDelta(text="x"), lease)
        assert await acquire_lease("acc", "S", rid + 1) is None
        await release_lease(lease)
    finally:
        engine_mod._sessionmaker = saved
        await engine.dispose()


def test_compose_worker_uses_only_the_pgbouncer_url():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
    block = compose[compose.index("\n  agent-worker:"):]
    block = block[:block.index("\nnetworks:")]
    code = "\n".join(ln for ln in block.splitlines() if not ln.strip().startswith("#"))
    assert "DIRECT_DATABASE_URL" not in code and "@postgres:" not in code
    assert "@pgbouncer:6432/" in code
    deps = code[code.index("depends_on:"):code.index("environment:")]
    assert "pgbouncer" in deps and "postgres" not in deps


def test_migration_018_is_additive_and_mirrored_in_init_sql():
    mig = (ROOT / "infra" / "sql" / "migrations" / "018_agent_session_leases.sql").read_text(
        encoding="utf-8")
    code = re.sub(r"--[^\n]*", "", mig)
    assert "CREATE TABLE IF NOT EXISTS agent_session_leases" in code and "fence BIGINT" in code
    assert not re.search(r"\b(DROP|DELETE|TRUNCATE|ALTER)\b", code, re.I)
    init = (ROOT / "infra" / "sql" / "init.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS agent_session_leases" in init
    assert "PRIMARY KEY (account_id, session_id)" in init and "fence BIGINT" in init


# --- database clock -----------------------------------------------------------------------------------


@pytest.mark.skipif(ON_SQLITE, reason="the database clock is a Postgres feature (SQLite branch uses the app clock)")
@pytest.mark.parametrize("skew", [timedelta(days=2), -timedelta(days=2)])
async def test_lease_is_judged_by_the_database_clock_not_the_app_clock(monkeypatch, skew):
    from aibroker.agent import lock as lock_mod

    def boom():
        raise AssertionError("the app clock was consulted on Postgres")

    monkeypatch.setattr(lock_mod, "_now", boom)               # any use of the app clock fails
    first = await acquire_lease("a", "live", 1, ttl_s=60)
    assert first is not None
    assert await acquire_lease("a", "live", 2, ttl_s=60) is None     # live by the DB clock
    short = await acquire_lease("a", "short", 1, ttl_s=0.3)
    await asyncio.sleep(0.5)
    assert await acquire_lease("a", "short", 2, ttl_s=60) is not None  # expired by the DB clock
    assert not await renew_lease(short)
    async with get_session() as s:
        gap = (await s.execute(text(
            "SELECT extract(epoch FROM (lease_until - (now() AT TIME ZONE 'utc'))) "
            "FROM agent_session_leases WHERE session_id = 'live'"))).scalar_one()
    assert 50 < float(gap) <= 60                               # stored on the DB timeline


async def test_sqlite_branch_uses_the_app_clock_for_unit_tests(monkeypatch):
    if not ON_SQLITE:
        pytest.skip("SQLite-only branch")
    from aibroker.agent import lock as lock_mod
    base = lock_mod._now()
    monkeypatch.setattr(lock_mod, "_now", lambda: base + timedelta(days=1))
    assert await acquire_lease("a", "s", 1, ttl_s=60) is not None
    monkeypatch.setattr(lock_mod, "_now", lambda: base + timedelta(days=3))
    assert await acquire_lease("a", "s", 2, ttl_s=60) is not None      # app clock moved past it


# --- a lost lease stops the CLI at once -----------------------------------------------------------------


async def _steal(account="acc", session="SL", run_id=4242):
    async with get_session() as s:
        row = (await s.execute(select(AgentSessionLeaseRow).where(
            AgentSessionLeaseRow.account_id == account,
            AgentSessionLeaseRow.session_id == session))).scalar_one()
        row.run_id = run_id
        row.fence = row.fence + 1


async def _wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_heartbeat_sets_lost_when_the_renewal_is_refused():
    async with session_lock("a", "s", 1, ttl_s=0.9, heartbeat_s=0.1) as lease:
        assert not lease.lost.is_set()
        await _steal("a", "s", 4242)
        assert await _wait_until(lease.lost.is_set, 1.0)


# The in-memory SQLite test DB has ONE shared connection: the lease heartbeat (every 0.1 s)
# and the executor's event writes interleave their transactions on it and collide on the
# event seq (CI: IntegrityError UNIQUE run_id, seq). Postgres gives each session its own
# connection; the CI integration job runs this there.
async def test_refused_renewal_stops_the_cli_within_a_heartbeat(tmp_path):
    rid = await _resume_run()
    fake = FakeAgentCLI([ev.MessageDelta(text="working"), ("hang",)])
    register_adapter("agent:claude", lambda: fake)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True,
                     cancel_poll_s=0.05, lease_ttl_s=5.0, lease_heartbeat_s=0.1)
    task = asyncio.create_task(execute_run(rid, cfg))
    if not await _wait_until(lambda: fake.started is not None):
        async with get_session() as s:
            job = await s.get(DeepJobRow, rid)
        detail = (task.done() and not task.cancelled() and repr(task.exception()), job.status,
                  job.run_after, job.error_message, await _events(rid))
        task.cancel()
        pytest.fail(f"run never started the CLI: {detail}")
    t0 = time.monotonic()
    await _steal()                                              # a newer holder: renewal -> False
    assert await asyncio.wait_for(task, 8) is None
    # Stopped by the refused heartbeat renewal, well before the 5 s TTL could lapse; the
    # half-TTL bound proves that without depending on a fast CI runner (was < 1.0 s, flaky).
    assert time.monotonic() - t0 < 2.5 and fake.cancel_calls == 1
    assert not set(await _events(rid)) & ev.TERMINAL_TYPES      # no terminal write by the zombie
    async with get_session() as s:
        assert (await s.get(DeepJobRow, rid)).status == "running"


# Same single-shared-connection effect as above: a wall-clock window around the TTL on SQLite
# depends on heartbeat/event write interleaving; the integration job runs it on Postgres.
@pytest.mark.parametrize("failure", ["raises", "hangs"])
async def test_db_errors_past_the_ttl_stop_the_cli(tmp_path, monkeypatch, failure):
    from aibroker.agent import lock as lock_mod
    rid = await _resume_run()
    fake = FakeAgentCLI([("hang",)])
    register_adapter("agent:claude", lambda: fake)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True,
                     cancel_poll_s=0.05, lease_ttl_s=0.6, lease_heartbeat_s=0.1)

    async def broken(lease, ttl_s=0.0):
        if failure == "hangs":
            await asyncio.sleep(3600)
        raise OSError("database unreachable")

    cancel_at = None
    original_cancel = fake.cancel

    async def timed_cancel():
        nonlocal cancel_at
        cancel_at = time.monotonic()
        await original_cancel()

    monkeypatch.setattr(fake, "cancel", timed_cancel)

    task = asyncio.create_task(execute_run(rid, cfg))
    assert await _wait_until(lambda: fake.started is not None)
    t0 = time.monotonic()
    monkeypatch.setattr(lock_mod, "renew_lease", broken)         # the DB goes away
    assert await asyncio.wait_for(task, 4) is None
    assert cancel_at is not None
    elapsed = cancel_at - t0
    assert 0.1 < elapsed < 0.6 + 0.8 and fake.cancel_calls == 1  # CLI stopped near the deadline
    assert not set(await _events(rid)) & ev.TERMINAL_TYPES


async def test_healthy_renewals_never_set_lost(tmp_path):
    rid = await _resume_run()
    fake = FakeAgentCLI([("sleep", 1.0), ev.RunCompleted(final_text="ok")])
    register_adapter("agent:claude", lambda: fake)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True,
                     cancel_poll_s=0.05, lease_ttl_s=0.6, lease_heartbeat_s=0.1)
    assert await execute_run(rid, cfg) == "completed" and fake.cancel_calls == 0


# --- the workspace write is fenced ------------------------------------------------------------------------


async def test_workspace_write_is_fenced(tmp_path):
    from aibroker.agent.executor import _drive
    from aibroker.db.models import AgentRunRow
    rid = await _resume_run()
    stale = await acquire_lease("acc", "SL", rid, ttl_s=-5)
    assert await acquire_lease("acc", "SL", 8888, ttl_s=60) is not None
    async with get_session() as s:
        run, job = await s.get(AgentRunRow, rid), await s.get(DeepJobRow, rid)
    cfg = ExecConfig(workspace_root=str(tmp_path), env={}, paid_continuation_verified_off=True)
    with pytest.raises(StaleLease):
        await _drive(run, job, FakeAgentCLI([ev.RunCompleted()]), cfg, stale)
    async with get_session() as s:
        assert (await s.get(AgentRunRow, rid)).workspace is None
    assert await _events(rid) == []
