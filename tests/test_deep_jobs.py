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


async def _insert_job(
    job_id: int, *, status: str = "pending", phash: str | None = None,
    project_id: int = 1, capability: str = "vision",
    created_at: datetime | None = None,
) -> None:
    async with get_session() as s:
        row = DeepJobRow(id=job_id, project_id=project_id, capability=capability,
                         status=status, request=_REQ, payload_hash=phash)
        if created_at is not None:
            row.created_at = created_at
        s.add(row)


async def test_find_inflight_duplicate_matches_pending_and_running():
    h = payload_hash(1, "vision", _REQ)
    await _insert_job(11, status="pending", phash=h)
    assert await _find_inflight_duplicate(1, "vision", h) == 11
    await _insert_job(12, status="running", phash=h)
    # Newest in-flight row wins (created_at DESC) — either is a valid dedup
    # target; assert it found one of them.
    assert await _find_inflight_duplicate(1, "vision", h) in (11, 12)


async def test_find_inflight_duplicate_ignores_done_error_and_other_scope():
    h = payload_hash(1, "vision", _REQ)
    await _insert_job(21, status="done", phash=h)
    await _insert_job(22, status="error", phash=h)
    await _insert_job(23, status="pending", phash=h, project_id=2)
    await _insert_job(24, status="pending", phash=h, capability="chat:fast")
    assert await _find_inflight_duplicate(1, "vision", h) is None


async def test_find_inflight_duplicate_ignores_rows_outside_window():
    h = payload_hash(1, "vision", _REQ)
    old = (datetime.now(UTC).replace(tzinfo=None)
           - timedelta(seconds=deep_jobs._DEDUP_WINDOW_S + 60))
    await _insert_job(31, status="pending", phash=h, created_at=old)
    assert await _find_inflight_duplicate(1, "vision", h) is None


async def test_find_inflight_duplicate_degrades_when_column_missing():
    """Code deployed before migration 010: the lookup must not raise — it
    disables dedup for the process and returns None (plain-insert fallback)."""
    async with get_session() as s:
        await s.execute(text("ALTER TABLE deep_jobs DROP COLUMN payload_hash"))
    h = payload_hash(1, "vision", _REQ)
    assert await _find_inflight_duplicate(1, "vision", h) is None
    assert deep_jobs._dedup_available is False
    # Second call short-circuits without touching the DB.
    assert await _find_inflight_duplicate(1, "vision", h) is None


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
    first = await _submit(project)
    second = await _submit(project)
    assert first != second
    assert deep_jobs._dedup_available is False
