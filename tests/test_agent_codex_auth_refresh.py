"""Codex shares CODEX_HOME/auth.json; a token refresh must not overlap another run's start-up.
Only the non-secret `last_refresh` timestamp is ever read to decide. All fixtures are synthetic."""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from aibroker.agent import codex_cli, executor
from aibroker.agent import events as ev
from aibroker.agent.adapter import AuthAttestation
from aibroker.agent.codex_cli import CodexCLI, read_last_refresh, refresh_maybe_due
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.lock import acquire_lease
from aibroker.agent.real_cli import SubprocessCLI
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import create_run, list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow

THREADS = ["123e4567-e89b-12d3-a456-426614174010", "123e4567-e89b-12d3-a456-426614174011"]
SECRET = "SECRET-TOKEN-VALUE"


@pytest.fixture(autouse=True)
def _adapters(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    clear_adapters()
    yield
    clear_adapters()


@pytest.fixture(autouse=True)
def _take_turns_on_the_db(serialized_db):
    """Concurrent runs share the single-connection test DB: see conftest.serialized_db."""


def _auth(home, last_refresh=None, raw=None):
    home.mkdir(exist_ok=True)
    body = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None, "tokens": {"refresh_token": SECRET}}
    if last_refresh is not None:
        body["last_refresh"] = last_refresh
    (home / "auth.json").write_text(raw if raw is not None else json.dumps(body), encoding="utf-8")


def _ago(**kw):
    return (datetime.now(UTC) - timedelta(**kw)).isoformat().replace("+00:00", "Z")


class _Codex(CodexCLI):
    """The REAL CodexCLI (refresh check, gate) with the subprocess stubbed out."""

    async def billing_mode(self):
        return "subscription"

    def attestation(self):
        return AuthAttestation("chatgpt", "firstParty")

    async def list_models(self):
        return None


async def _runs(n, base=9400):
    ids = []
    for i in range(n):
        plain = generate_project_key()
        async with get_session() as s:
            await s.execute(insert(ProjectRow).values(
                id=base + i, name=f"auth_{base + i}", project_key_hash=hash_project_key(plain),
                project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
        rid, _ = await create_run(
            project=SimpleNamespace(id=base + i), capability="agent:codex", prompt="p", task_id="t",
            account_id="a", session_id=None, idempotency_key=None, wall_timeout_s=30,
            idle_timeout_s=30, allowed_tools=[])
        async with get_session() as s:
            (await s.get(DeepJobRow, rid)).status = "running"
        ids.append(rid)
    return ids


def _cfg(tmp_path, **kw):
    kw.setdefault("codex_auth_poll_s", 0.01)
    return ExecConfig(workspace_root=str(tmp_path / "w"), env={}, codex_paid_continuation_verified_off=True,
                      **kw)


async def _until(pred, timeout=10.0):
    """Condition-based wait (no fixed sleeps): poll a predicate until it holds."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.002)


class _Rig:
    """Stubs the CLI subprocess and counts lease attempts, so waiting is observed, not slept."""

    def __init__(self, monkeypatch, ids, *, first_event="session"):
        self.ids = ids
        self.entered: list[int] = []
        self.gates = {rid: asyncio.Event() for rid in ids}        # released -> the CLI emits
        self.session_seen = {rid: asyncio.Event() for rid in ids}
        self.attempts: list[int] = []                              # one per refresh-lease attempt
        self.first_event = first_event
        self.released: list[int] = []         # runs whose gate was closed (lease released)
        # The single-connection test DB cannot take two runs' writes at once. When set, a released
        # lease stays "held" until the test clears it after the releasing run has fully finished.
        self.keep_holder_after_release = False
        self.holder: int | None = None        # who holds the refresh lease (in memory, see below)
        real_lock = executor.session_lock

        @contextlib.asynccontextmanager
        async def fake_auth_lock(run_id):
            # The refresh lease is modelled in memory: the single-connection test DB cannot take
            # concurrent sessions deterministically. The real lease row is covered in
            # test_agent_lease.py; here only the gate logic is under test.
            self.attempts.append(run_id)
            if self.holder is not None:
                yield None
                return
            self.holder = run_id
            try:
                yield SimpleNamespace(run_id=run_id)
            finally:
                self.released.append(run_id)
                if not self.keep_holder_after_release:
                    self.holder = None

        def counting_lock(account, session, run_id=0, **kw):
            if account == "__codex_auth__":
                return fake_auth_lock(run_id)
            return real_lock(account, session, run_id, **kw)

        monkeypatch.setattr(executor, "session_lock", counting_lock)

        async def fake_start(sub_self, run):
            self.entered.append(run.run_id)
            if self.first_event == "session":
                await self.gates[run.run_id].wait()               # the CLI is "refreshing"
            yield ev.SessionAssigned(session_id=THREADS[ids.index(run.run_id)])
            self.session_seen[run.run_id].set()                   # the executor consumed it
            if self.first_event != "session":
                await self.gates[run.run_id].wait()               # first MODEL event not yet there
            yield ev.MessageDelta(text="hi")
            yield ev.RunCompleted(final_text="ok")

        monkeypatch.setattr(SubprocessCLI, "start", fake_start)

    def attempts_by(self, rid):
        return self.attempts.count(rid)

    def release_all(self):
        for g in self.gates.values():
            g.set()


def _register(home):
    register_adapter("agent:codex", lambda: _Codex(env={"CODEX_HOME": str(home)}))


@pytest.mark.parametrize("state", ["stale", "missing", "garbage", "no_field"])
async def test_refresh_due_runs_serialize_startup(tmp_path, monkeypatch, state):
    home = tmp_path / "home"
    if state == "stale":
        _auth(home, _ago(days=7))
    elif state == "garbage":
        _auth(home, raw="{not json")
    elif state == "no_field":
        _auth(home)
    ids = await _runs(2)
    rig = _Rig(monkeypatch, ids)
    rig.keep_holder_after_release = True
    _register(home)
    cfg = _cfg(tmp_path)
    t1 = asyncio.create_task(execute_run(ids[0], cfg))
    await _until(lambda: rig.entered == [ids[0]])        # run 1 holds the lease inside the CLI
    t2 = asyncio.create_task(execute_run(ids[1], cfg))
    await _until(lambda: rig.attempts_by(ids[1]) >= 3)   # run 2 was refused repeatedly: it waits
    assert rig.entered == [ids[0]]
    assert rig.holder == ids[0]
    rig.gates[ids[0]].set()
    assert await asyncio.wait_for(t1, 10) == "completed"
    assert rig.released == [ids[0]]                      # run 1 let go after its first model event
    rig.holder = None                                    # (run 1 is fully done: now hand it over)
    await _until(lambda: ids[1] in rig.entered)          # lease freed: run 2 may start
    rig.gates[ids[1]].set()
    assert await asyncio.wait_for(t2, 10) == "completed"
    assert rig.released == ids


async def test_lease_is_kept_past_session_assigned_until_a_model_event(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _auth(home)
    ids = await _runs(2, base=9440)
    rig = _Rig(monkeypatch, ids, first_event="model")
    rig.keep_holder_after_release = True
    _register(home)
    cfg = _cfg(tmp_path)
    t1 = asyncio.create_task(execute_run(ids[0], cfg))
    await _until(lambda: rig.session_seen[ids[0]].is_set())    # session.assigned was processed
    t2 = asyncio.create_task(execute_run(ids[1], cfg))
    await _until(lambda: rig.attempts_by(ids[1]) >= 3)
    assert rig.entered == [ids[0]] and rig.released == []       # still held: no model event yet
    rig.gates[ids[0]].set()                                     # first message.delta arrives
    assert await asyncio.wait_for(t1, 10) == "completed"
    assert rig.released == [ids[0]]
    rig.holder = None
    await _until(lambda: ids[1] in rig.entered)
    rig.release_all()
    assert await asyncio.wait_for(t2, 10) == "completed"


async def test_fresh_login_takes_no_lease_and_runs_stay_parallel(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _auth(home, _ago(hours=1))
    ids = await _runs(2, base=9410)
    rig = _Rig(monkeypatch, ids)
    _register(home)
    t1 = asyncio.create_task(execute_run(ids[0], _cfg(tmp_path)))
    await _until(lambda: rig.entered == [ids[0]])       # staggered: the test DB has one connection
    t2 = asyncio.create_task(execute_run(ids[1], _cfg(tmp_path)))
    await _until(lambda: sorted(rig.entered) == sorted(ids))   # both in flight at once
    assert rig.attempts == []                           # the refresh lease was never even tried
    assert rig.holder is None
    rig.gates[ids[0]].set()
    assert await asyncio.wait_for(t1, 10) == "completed"
    rig.gates[ids[1]].set()
    assert await asyncio.wait_for(t2, 10) == "completed"


async def test_startup_lease_is_bounded_when_the_cli_emits_nothing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _auth(home)                                      # refresh due
    ids = await _runs(2, base=9420)
    rig = _Rig(monkeypatch, ids)
    _register(home)
    cfg = _cfg(tmp_path, codex_auth_startup_s=0.05)  # injected cap instead of the real 60 s
    t1 = asyncio.create_task(execute_run(ids[0], cfg))
    await _until(lambda: rig.entered == [ids[0]])
    t2 = asyncio.create_task(execute_run(ids[1], cfg))
    await _until(lambda: ids[1] in rig.entered)      # the cap freed the lease though run 1 is silent
    rig.release_all()
    assert await asyncio.wait_for(asyncio.gather(t1, t2), 10) == ["completed", "completed"]


async def test_a_run_that_cannot_get_the_lease_in_time_is_requeued_without_events(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _auth(home)
    (rid,) = await _runs(1, base=9430)
    rig = _Rig(monkeypatch, [rid])
    rig.holder = 4242                                # another run holds the refresh lease
    _register(home)
    assert await execute_run(rid, _cfg(tmp_path, codex_auth_wait_s=0.02)) is None
    assert rig.entered == []
    assert await list_events(rid) == []              # nothing written, so nothing repeats on retry
    async with get_session() as s:
        row = await s.get(DeepJobRow, rid)
        assert row.status == "pending" and row.retry_count == 1


async def test_requeues_are_capped_with_a_clear_error_class(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _auth(home)
    (rid,) = await _runs(1, base=9450)
    _Rig(monkeypatch, [rid]).holder = 4242
    _register(home)
    cfg = _cfg(tmp_path, codex_auth_wait_s=0.02, session_busy_max=0)
    assert await execute_run(rid, cfg) == "failed"
    failed = [e for e in await list_events(rid) if e.type == "run.failed"]
    assert len(failed) == 1 and failed[0].payload["error_class"] == "codex_auth_busy"


async def test_real_lease_row_requeues_without_events_then_fails_after_the_cap(tmp_path, monkeypatch):
    """Through the REAL session_lock / lease table (no fake), no concurrency needed."""
    home = tmp_path / "home"
    _auth(home)                                      # refresh due
    (rid,) = await _runs(1, base=9460)
    assert await acquire_lease("__codex_auth__", "refresh", 4242, ttl_s=60)   # another worker holds it
    started: list[int] = []

    async def fake_start(sub_self, run):
        started.append(run.run_id)
        yield ev.RunCompleted(final_text="ok")

    monkeypatch.setattr(SubprocessCLI, "start", fake_start)
    _register(home)
    cfg = _cfg(tmp_path, codex_auth_wait_s=0.02, session_busy_max=1)
    assert await execute_run(rid, cfg) is None       # requeued
    assert started == [] and await list_events(rid) == []
    async with get_session() as s:
        row = await s.get(DeepJobRow, rid)
        assert row.status == "pending" and row.retry_count == 1
        row.status = "running"                       # claimed again
    assert await execute_run(rid, cfg) == "failed"   # 2nd refusal exceeds session_busy_max=1
    events = await list_events(rid)
    assert [e.type for e in events] == ["run.failed"]
    assert events[0].payload["error_class"] == "codex_auth_busy" and started == []
    assert await acquire_lease("__codex_auth__", "refresh", 1) is None    # still the other holder's


def test_only_the_timestamp_key_is_read_never_a_token(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _auth(home, _ago(days=1))
    touched: list[str] = []

    class Spy(dict):
        def get(self, key, default=None):
            touched.append(key)
            return super().get(key, default)

        def __getitem__(self, key):
            touched.append(key)
            return super().__getitem__(key)

        def items(self):
            touched.append("<items>")
            return super().items()

        def values(self):
            touched.append("<values>")
            return super().values()

    real = json.loads
    monkeypatch.setattr(codex_cli.json, "loads", lambda s, *a, **k: Spy(real(s, *a, **k)))
    assert read_last_refresh(str(home)) is not None
    assert refresh_maybe_due(str(home)) is False
    assert set(touched) == {"last_refresh"}


def test_timestamp_parsing_and_conservative_defaults(tmp_path):
    home = tmp_path / "home"
    now = datetime(2026, 10, 13, tzinfo=UTC)
    _auth(home, "2000-01-02T03:04:05.123456789Z")        # synthetic; nanoseconds + Z
    assert read_last_refresh(str(home)) == datetime(2000, 1, 2, 3, 4, 5, 123456, tzinfo=UTC)
    assert refresh_maybe_due(str(home), now) is True      # years old
    _auth(home, "2026-10-12T00:00:00Z")
    assert refresh_maybe_due(str(home), now) is False
    _auth(home, "2099-01-01T00:00:00Z")                   # clock skew: not due
    assert refresh_maybe_due(str(home), now) is False
    _auth(home, "yesterday")
    assert read_last_refresh(str(home)) is None and refresh_maybe_due(str(home), now) is True
    _auth(home, 12345)                                    # wrong type
    assert read_last_refresh(str(home)) is None
    assert refresh_maybe_due(None) is True and refresh_maybe_due(str(tmp_path / "nope")) is True


def test_oversized_or_pathological_auth_json_is_unreadable_and_counts_as_due(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    pad = "x" * (1 << 20)
    (home / "auth.json").write_text(json.dumps({"last_refresh": _ago(hours=1), "pad": pad}),
                                    encoding="utf-8")
    assert read_last_refresh(str(home)) is None and refresh_maybe_due(str(home)) is True
    _auth(home, _ago(hours=1))

    def boom(*a, **k):
        raise RecursionError

    with monkeypatch.context() as m:
        m.setattr(codex_cli.json, "loads", boom)
        assert read_last_refresh(str(home)) is None
    (home / "auth.json").write_bytes(b"\xff\xfe not utf-8")
    assert read_last_refresh(str(home)) is None


async def test_a_cancel_while_waiting_for_the_gate_requeues_at_once(tmp_path, monkeypatch):
    """No 90 s wait for a cancelled run: it is requeued and the next claim ends it `cancelled`."""
    home = tmp_path / "home"
    _auth(home)
    (rid,) = await _runs(1, base=9470)
    rig = _Rig(monkeypatch, [rid])
    rig.holder = 4242                                # another run holds the refresh lease

    async def cancelled(run_id):
        return True

    monkeypatch.setattr(executor, "_cancel_flag", cancelled)
    _register(home)
    t0 = asyncio.get_running_loop().time()
    assert await execute_run(rid, _cfg(tmp_path, codex_auth_wait_s=30)) is None
    assert asyncio.get_running_loop().time() - t0 < 5
    assert rig.attempts_by(rid) == 1 and rig.entered == []
    assert await list_events(rid) == []


async def test_a_run_cancelled_before_it_got_the_gate_never_starts_the_cli(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _auth(home)                                      # refresh due -> the gate is taken
    (rid,) = await _runs(1, base=9480)
    rig = _Rig(monkeypatch, [rid])

    async def cancelled(run_id):
        return True

    monkeypatch.setattr(executor, "_cancel_flag", cancelled)
    _register(home)
    assert await execute_run(rid, _cfg(tmp_path)) == "cancelled"
    assert rig.entered == [] and rig.released == [rid]          # gate taken, then let go
    assert [e.type for e in await list_events(rid)] == ["run.cancelled"]
