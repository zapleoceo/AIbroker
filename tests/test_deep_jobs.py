"""In-flight job dedup — pure payload_hash canonicalization + duplicate lookup
(SQLite) + full submit_job dedup behaviour (Postgres-only: BIGSERIAL insert
path, see the pragma note in services/deep_jobs.py)."""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.services import deep_jobs
from aibroker.services.deep_jobs import _find_inflight_duplicate, payload_hash

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")

_REQ = {"messages": [{"role": "user", "content": "hi"}], "model": None,
        "max_tokens": 64, "temperature": 0.7, "response_format": None,
        "workflow": "t"}


@pytest.fixture(autouse=True)
def _reset_dedup_flag():
    yield
    deep_jobs._dedup_available = True


# ─── payload_hash — pure, SQLite-safe ────────────────────────────────────────


def test_payload_hash_ignores_dict_key_order():
    a = {"model": None, "messages": [{"role": "user", "content": "hi"}]}
    b = {"messages": [{"role": "user", "content": "hi"}], "model": None}
    assert payload_hash(4, "vision", a) == payload_hash(4, "vision", b)


def test_payload_hash_differs_by_project_and_capability():
    h = payload_hash(4, "vision", _REQ)
    assert payload_hash(5, "vision", _REQ) != h
    assert payload_hash(4, "chat:fast", _REQ) != h


def test_payload_hash_differs_by_request_content():
    other = dict(_REQ, max_tokens=65)
    assert payload_hash(4, "vision", other) != payload_hash(4, "vision", _REQ)


def test_payload_hash_is_md5_hex():
    h = payload_hash(1, "vision", _REQ)
    assert len(h) == 32
    int(h, 16)  # valid hex or this raises


# ─── _find_inflight_duplicate — runs on SQLite via explicit-id inserts ───────


# Explicit high ids: SQLite can't autoincrement the BigInteger PK, and on
# Postgres deep_jobs.project_id carries a REAL FK — hardcoded project_id=1
# violated it in CI (2026-07-16). Insert the parent project explicitly too,
# far above anything the sequence will hand out.
_P1, _P2 = 990001, 990002


async def _ensure_project(pid: int) -> None:
    async with get_session() as s:
        if await s.get(ProjectRow, pid) is None:
            s.add(ProjectRow(id=pid, name=f"dedup-fixture-{pid}",
                             project_key_hash=f"h{pid}",
                             project_key_prefix="pk_x",
                             allowed_scopes=["llm:chat"]))


async def _insert_job(
    job_id: int, *, status: str = "pending", phash: str | None = None,
    project_id: int = _P1, capability: str = "vision",
    created_at: datetime | None = None,
) -> None:
    await _ensure_project(project_id)
    async with get_session() as s:
        row = DeepJobRow(id=job_id, project_id=project_id, capability=capability,
                         status=status, request=_REQ, payload_hash=phash)
        if created_at is not None:
            row.created_at = created_at
        s.add(row)


async def test_find_inflight_duplicate_matches_pending_and_running():
    h = payload_hash(_P1, "vision", _REQ)
    await _insert_job(11, status="pending", phash=h)
    assert await _find_inflight_duplicate(_P1, "vision", h) == 11
    await _insert_job(12, status="running", phash=h)
    # Newest in-flight row wins (created_at DESC) — either is a valid dedup
    # target; assert it found one of them.
    assert await _find_inflight_duplicate(_P1, "vision", h) in (11, 12)


async def test_find_inflight_duplicate_ignores_done_error_and_other_scope():
    h = payload_hash(_P1, "vision", _REQ)
    await _insert_job(21, status="done", phash=h)
    await _insert_job(22, status="error", phash=h)
    await _insert_job(23, status="pending", phash=h, project_id=_P2)
    await _insert_job(24, status="pending", phash=h, capability="chat:fast")
    assert await _find_inflight_duplicate(_P1, "vision", h) is None


async def test_find_inflight_duplicate_ignores_rows_outside_window():
    h = payload_hash(_P1, "vision", _REQ)
    old = (datetime.now(UTC).replace(tzinfo=None)
           - timedelta(seconds=deep_jobs._DEDUP_WINDOW_S + 60))
    await _insert_job(31, status="pending", phash=h, created_at=old)
    assert await _find_inflight_duplicate(_P1, "vision", h) is None


async def test_find_inflight_duplicate_degrades_when_column_missing():
    """Code deployed before migration 010: the lookup must not raise — it
    disables dedup for the process and returns None (plain-insert fallback).
    The column is restored in `finally`: CI shares one database across the
    whole session, so leaving it dropped poisons every later test."""
    async with get_session() as s:
        await s.execute(text("ALTER TABLE deep_jobs DROP COLUMN payload_hash"))
    try:
        h = payload_hash(_P1, "vision", _REQ)
        assert await _find_inflight_duplicate(_P1, "vision", h) is None
        assert deep_jobs._dedup_available is False
        # Second call short-circuits without touching the DB.
        assert await _find_inflight_duplicate(_P1, "vision", h) is None
    finally:
        async with get_session() as s:
            await s.execute(
                text("ALTER TABLE deep_jobs ADD COLUMN payload_hash VARCHAR(32)"))


# ─── submit_job dedup — Postgres only (BIGSERIAL autoincrement insert) ───────


async def _make_project() -> ProjectRow:
    async with get_session() as s:
        p = ProjectRow(
            name=f"dedup-{os.urandom(4).hex()}", project_key_hash="h",
            project_key_prefix="pk_x", allowed_scopes=["llm:chat"],
        )
        s.add(p)
        await s.flush()
        pid = p.id
    async with get_session() as s:
        return await s.get(ProjectRow, pid)


async def _submit(project: ProjectRow, content: str = "hi") -> int:
    return await deep_jobs.submit_job(
        project=project, capability="chat:fast",
        messages=[{"role": "user", "content": content}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="t",
    )


@pytest.mark.skipif(ON_SQLITE, reason="submit_job insert path needs Postgres BIGSERIAL")
async def test_submit_same_payload_twice_returns_same_id_single_row():
    project = await _make_project()
    with patch.object(deep_jobs, "_notify_dispatcher", AsyncMock()) as notify:
        first = await _submit(project)
        second = await _submit(project)
    assert first == second
    # The dup return must not re-wake the dispatcher — only the real enqueue does.
    assert notify.call_count == 1
    async with get_session() as s:
        n = (await s.execute(text(
            "SELECT count(*) FROM deep_jobs WHERE project_id = :p"
        ), {"p": project.id})).scalar_one()
    assert n == 1


@pytest.mark.skipif(ON_SQLITE, reason="submit_job insert path needs Postgres BIGSERIAL")
async def test_submit_different_payload_gets_new_id():
    project = await _make_project()
    assert await _submit(project, "hi") != await _submit(project, "bye")


@pytest.mark.skipif(ON_SQLITE, reason="submit_job insert path needs Postgres BIGSERIAL")
async def test_done_job_with_same_hash_does_not_dedup():
    """Only in-flight jobs dedup — after done/error the client may
    legitimately want a fresh answer (retry after failure)."""
    project = await _make_project()
    first = await _submit(project)
    async with get_session() as s:
        await s.execute(text("UPDATE deep_jobs SET status='done' WHERE id=:i"),
                        {"i": first})
    assert await _submit(project) != first


@pytest.mark.skipif(ON_SQLITE, reason="submit_job insert path needs Postgres BIGSERIAL")
async def test_inflight_job_outside_window_does_not_dedup():
    project = await _make_project()
    first = await _submit(project)
    async with get_session() as s:
        await s.execute(text(
            "UPDATE deep_jobs SET created_at = now() - make_interval(secs => :old) "
            "WHERE id = :i"
        ), {"old": deep_jobs._DEDUP_WINDOW_S + 60, "i": first})
    assert await _submit(project) != first


@pytest.mark.skipif(ON_SQLITE, reason="submit_job insert path needs Postgres BIGSERIAL")
async def test_submit_degrades_to_plain_insert_when_column_missing():
    """Deploying the code before applying migration 010 must not 500: dedup
    disables itself and identical submits fall back to duplicate rows."""
    project = await _make_project()
    async with get_session() as s:
        await s.execute(text("ALTER TABLE deep_jobs DROP COLUMN payload_hash"))
    try:
        first = await _submit(project)
        second = await _submit(project)
        assert first != second
        assert deep_jobs._dedup_available is False
    finally:
        async with get_session() as s:
            await s.execute(
                text("ALTER TABLE deep_jobs ADD COLUMN payload_hash VARCHAR(32)"))


async def test_get_job_poll_view_matches_full_row_and_skips_prompt_and_audio():
    """get_job reads three JSON paths, not the whole `request`: the poll response must
    be identical to the one built from the full row, for plain, subscription and
    transcription jobs, in every status."""
    from aibroker.routes import proxy

    pid = 984000
    async with get_session() as s:
        s.add(ProjectRow(id=pid, name="pollview", project_key_hash="h",
                         project_key_prefix="pk_pv", allowed_scopes=["llm:chat"]))
    big = "x" * 5000
    requests = {
        "plain": {"messages": [{"role": "user", "content": big}], "model": "gpt-oss-120b",
                  "max_tokens": 64, "temperature": 0.7, "workflow": "t"},
        "subscription": {"prompt": big, "account_id": "owner", "model": "gpt-5", "task_id": "jobs",
                         "client_job": {"capability": "chat:fast", "provider": "codex",
                                        "model": "codex/gpt-5", "tools": [], "tool_choice": None}},
        "transcription": {"audio_b64": big, "filename": "a.ogg", "messages": [], "model": None},
    }
    jid = 984100
    for name, req in requests.items():
        for status in ("pending", "running", "done", "error", "cancelled"):
            jid += 1
            async with get_session() as s:
                s.add(DeepJobRow(
                    id=jid, project_id=pid, status=status, request=req,
                    result_text="ok" if status == "done" else None,
                    error_message="boom" if status == "error" else None,
                    result_meta={"provider": "codex", "model": "m", "tokens_in": 3,
                                 "account_id": "owner", "model_served_kind": "exact"}
                    if name == "subscription" else {"provider": "p", "model": "m"},
                    trace_id="t-1", labels={"a": "b"}, cancel_requested=status == "running"))
            async with get_session() as s:
                full = await s.get(DeepJobRow, jid)
                expected = proxy._job_response(full)
                policy_args = (full.request.get("model"), full.request.get("account_id", ""),
                               full.capability)
            view = await deep_jobs.get_job(jid, pid)
            # these fixture rows have no completed_at, so queued_ms/run_ms grow with the clock
            # (timings: tests/test_job_status.py); compare the rest exactly
            moving = {"queued_ms", "run_ms"}
            assert (proxy._job_response(view).model_dump(exclude=moving)
                    == expected.model_dump(exclude=moving)), (name, status)
            assert (view.request.get("model"), view.request.get("account_id", ""),
                    view.capability) == policy_args
            assert set(view.request) <= {"model", "account_id", "client_job"}
    assert await deep_jobs.get_job(jid, pid + 1) is None        # scoped to the project
    assert await deep_jobs.get_job(1, pid) is None


async def _fin_row(job_id: int, request: dict, started_at: datetime | None) -> None:
    await _ensure_project(_P1)
    async with get_session() as s:
        s.add(DeepJobRow(id=job_id, project_id=_P1, capability="chat:fast", status="running",
                         request=request, started_at=started_at))


async def test_finish_single_update_strips_audio_keeps_rest_and_sets_result():
    started = datetime(2026, 10, 13, 9, 30, 1, 654321)
    await _fin_row(984201, {"audio_b64": "QUJD", "filename": "a.ogg", "model": None}, started)
    await deep_jobs._finish(984201, status="done", result_text="hello",
                            result_meta={"provider": "p"}, expect_started_at=started)
    async with get_session() as s:
        row = await s.get(DeepJobRow, 984201)
    assert (row.status, row.result_text, row.result_meta) == ("done", "hello", {"provider": "p"})
    assert row.error_message is None and row.completed_at is not None
    assert row.request == {"audio_b64": None, "filename": "a.ogg", "model": None}


async def test_finish_leaves_request_without_audio_untouched():
    req = {"messages": [{"role": "user", "content": "hi"}], "audio_b64": "", "model": "m"}
    await _fin_row(984202, req, None)
    await _fin_row(984203, {"messages": []}, None)
    await deep_jobs._finish(984202, status="error", error_message="boom")
    await deep_jobs._finish(984203, status="done", result_text="t")
    async with get_session() as s:
        a, b = await s.get(DeepJobRow, 984202), await s.get(DeepJobRow, 984203)
    assert a.request == req and (a.status, a.error_message) == ("error", "boom")
    assert b.request == {"messages": []} and b.result_text == "t"


async def test_finish_without_meta_writes_sql_null_not_json_null():
    await _fin_row(984204, {"messages": []}, None)
    await deep_jobs._finish(984204, status="error", error_message="boom")
    async with get_session() as s:
        is_null = (await s.execute(text(
            "SELECT result_meta IS NULL FROM deep_jobs WHERE id = 984204"))).scalar_one()
    assert is_null                       # as the ORM path wrote it; JSON 'null' would be False


async def test_finish_fence_a_stale_worker_does_not_clobber_the_live_claim():
    ours = datetime(2026, 10, 13, 9, 30, 1, 654321)
    theirs = datetime(2026, 10, 13, 9, 45, 0, 1)
    await _fin_row(984204, {"audio_b64": "QUJD"}, theirs)       # re-claimed by another worker
    await _fin_row(984205, {"audio_b64": "QUJD"}, None)         # re-queued, not yet re-claimed
    for jid in (984204, 984205):
        await deep_jobs._finish(jid, status="done", result_text="stale", expect_started_at=ours)
    async with get_session() as s:
        for jid in (984204, 984205):
            row = await s.get(DeepJobRow, jid)
            assert row.status == "running" and row.result_text is None
            assert row.request == {"audio_b64": "QUJD"} and row.completed_at is None
    await deep_jobs._finish(984299, status="done")              # vanished row: silent no-op


def test_strip_audio_sql_has_a_postgres_and_a_sqlite_branch():
    pg, lite = deep_jobs._strip_audio_sql("postgresql"), deep_jobs._strip_audio_sql("sqlite")
    assert "jsonb_set(request, '{audio_b64}', 'null'::jsonb)" in pg and "->>" in pg
    assert "json_set(request, '$.audio_b64', json('null'))" in lite and "json_extract" in lite


async def test_submit_queues_the_notify_on_the_insert_session_before_commit():
    """The NOTIFY rides the INSERT's own transaction (Postgres delivers it at commit): it is
    called once, with the session that holds the flushed row, not from a second session."""
    from sqlalchemy.ext.asyncio import AsyncSession

    pid = 984300
    await _ensure_project(pid)
    async with get_session() as s:
        project = await s.get(ProjectRow, pid)
    seen: list[tuple[AsyncSession, bool, bool]] = []

    async def spy(session):
        seen.append((session, session.in_transaction(),
                     any(isinstance(o, DeepJobRow) for o in session.identity_map.values())))

    with patch.object(deep_jobs, "_notify_dispatcher", spy):
        outcome = await deep_jobs.submit_job_ex(
            project=project, capability="chat:fast",
            messages=[{"role": "user", "content": "notify-in-txn"}], model=None,
            max_tokens=64, temperature=0.7, response_format=None, workflow="t")
    assert len(seen) == 1 and isinstance(seen[0][0], AsyncSession)
    assert seen[0][1] and seen[0][2]
    async with get_session() as s:
        assert (await s.get(DeepJobRow, outcome.job_id)).status == "pending"


async def test_notify_failure_is_confined_to_a_savepoint_and_swallowed():
    from types import SimpleNamespace

    class _Sp:
        entered = exited_with = None

        async def __aenter__(self):
            _Sp.entered = True

        async def __aexit__(self, et, *_):
            _Sp.exited_with = et
            return False

    class _Session:
        bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def begin_nested(self):
            return _Sp()

        async def execute(self, stmt):
            assert "pg_notify" in str(stmt)
            raise RuntimeError("boom")

    await deep_jobs._notify_dispatcher(_Session())            # must not raise
    assert _Sp.entered and _Sp.exited_with is RuntimeError
    sqlite_like = SimpleNamespace(bind=SimpleNamespace(dialect=SimpleNamespace(name="sqlite")))
    await deep_jobs._notify_dispatcher(sqlite_like)           # no-op off Postgres
