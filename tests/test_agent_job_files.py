"""Agent job files: raw bytes in deep_job_files (migration 025), API JSON unchanged.

With AGENT_FILES_IN_TABLE on, binary files of a new job keep their bytes in the table and only
metadata + content_ref in result_meta; the served JSON must equal the legacy inline format for
live and expired files, rows written under either mode are readable under either flag value, and
the bytes die with the job. SQLite-runnable; the retention purge runs on Postgres."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, text, update
from test_agent_runs import _get, _project, _submit

import aibroker.db.engine as engine_mod
from aibroker.agent import events as ev
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.job_files import store_files
from aibroker.agent.runs import RunConflict, append_event
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobFileRow, DeepJobRow
from aibroker.routes import proxy

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 40
OTHER = b"hello \x00\xff binary"


@pytest.fixture
def flag(monkeypatch):
    def set_to(value: bool) -> None:
        monkeypatch.setattr(get_settings(), "AGENT_FILES_IN_TABLE", value)
    set_to(True)
    return set_to


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


def _file(path: str, data: bytes, expires: timedelta = timedelta(hours=1)) -> ev.FileRef:
    return ev.FileRef(path=path, size=len(data), content_b64=base64.b64encode(data).decode(),
                      expires_at=_iso(expires), sha256=hashlib.sha256(data).hexdigest())


def _legacy_served(dump: dict) -> dict:
    """The previous reader, verbatim: inline content served, expired blanked."""
    now = datetime.now(UTC).isoformat()
    if dump.get("expires_at") and dump["expires_at"] < now:
        return {**dump, "content_b64": None, "expired": True}
    return dump


async def _new_run() -> tuple[str, int]:
    key, _ = await _project([AGENT_SCOPE])
    return key, (await _submit(key)).json()["run_id"]


async def _legacy_run(files: list[ev.FileRef]) -> tuple[str, int]:
    """A run finished the way the code did before: content_b64 inline in result_meta."""
    key, rid = await _new_run()
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        job.status, job.result_text = "done", "ok"
        job.result_meta = {"files": [f.model_dump() for f in files]}
        await s.execute(update(AgentRunRow).where(AgentRunRow.run_id == rid).values(outcome="completed"))
    return key, rid


async def _meta(rid: int) -> dict:
    async with get_session() as s:
        return (await s.get(DeepJobRow, rid)).result_meta


async def _rows(rid: int) -> list[DeepJobFileRow]:
    async with get_session() as s:
        return list((await s.execute(select(DeepJobFileRow).where(DeepJobFileRow.job_id == rid)
                                     .order_by(DeepJobFileRow.idx))).scalars())


async def test_flag_on_stores_binary_bytes_in_the_table_and_not_inline(flag):
    _, rid = await _new_run()
    skipped = ev.FileRef(path="big.bin", size=9, skipped="too_large")
    text_file = _file("notes.txt", b"plain text")
    await append_event(rid, ev.RunCompleted(
        final_text="ok", files=[skipped, _file("out/a.png", PNG), text_file]))
    meta = await _meta(rid)
    assert meta["files"][0] == skipped.model_dump()                    # no content: untouched
    stored = meta["files"][1]
    assert stored["content_b64"] is None and stored["content_ref"] == 1  # idx = list position
    assert "iVBOR" not in json.dumps(meta)
    assert meta["files"][2] == text_file.model_dump()                  # compressible text stays inline
    (row,) = await _rows(rid)
    assert (row.idx, row.data, row.mime, row.sha256) == (
        1, PNG, "image/png", hashlib.sha256(PNG).hexdigest())


async def test_flag_off_writes_inline_exactly_as_before():
    _, rid = await _new_run()
    f = _file("out/a.png", PNG)
    await append_event(rid, ev.RunCompleted(final_text="ok", files=[f]))
    assert (await _meta(rid))["files"] == [f.model_dump()] and await _rows(rid) == []


async def test_size_accounting_is_the_decoded_base64_length(flag):
    _, rid = await _new_run()
    files = [_file("out/a.png", PNG), _file("b.jpg", OTHER)]
    await append_event(rid, ev.RunCompleted(final_text="ok", files=files))
    rows = await _rows(rid)
    assert len(rows) == 2
    for row, f in zip(rows, files, strict=True):
        assert row.size == len(row.data) == len(base64.b64decode(f.content_b64)) == f.size
    assert rows[1].mime == "image/jpeg"
    assert sum(r.size for r in rows) < sum(len(f.content_b64) for f in files)   # ~25% less than base64


async def test_api_response_is_identical_to_the_legacy_inline_format_live_and_expired(flag):
    files = [_file("out/live.png", PNG), _file("old.png", OTHER, expires=timedelta(hours=-1)),
             ev.FileRef(path="skip.bin", size=3, skipped="cap_reached")]
    new_key, new_id = await _new_run()
    await append_event(new_id, ev.RunCompleted(final_text="ok", files=files))
    old_key, old_id = await _legacy_run(files)

    new_files = (await _get(new_key, f"/{new_id}")).json()["files"]
    old_files = (await _get(old_key, f"/{old_id}")).json()["files"]
    assert new_files == old_files == [_legacy_served(f.model_dump()) for f in files]
    assert json.dumps(new_files, sort_keys=True) == json.dumps(old_files, sort_keys=True)
    assert base64.b64decode(new_files[0]["content_b64"]) == PNG
    assert new_files[1]["content_b64"] is None and new_files[1]["expired"] is True
    assert "content_ref" not in json.dumps(new_files)


async def test_jobs_poll_route_shape_serves_the_same_files(flag):
    files = [_file("out/live.png", PNG), _file("old.png", OTHER, expires=timedelta(hours=-1))]
    _, new_id = await _new_run()
    await append_event(new_id, ev.RunCompleted(final_text="ok", files=files))
    _, old_id = await _legacy_run(files)
    async with get_session() as s:
        new_row, old_row = await s.get(DeepJobRow, new_id), await s.get(DeepJobRow, old_id)
    new_resp = proxy._job_response(new_row, await proxy._row_files(new_row))
    old_resp = proxy._job_response(old_row)                       # inline path, sync as before
    assert new_resp.files == old_resp.files == [_legacy_served(f.model_dump()) for f in files]
    assert proxy._job_response(old_row, await proxy._row_files(old_row)).files == old_resp.files


async def test_legacy_inline_rows_are_still_served_unchanged(flag):
    f = _file("out/a.png", PNG)
    key, rid = await _legacy_run([f])
    assert (await _get(key, f"/{rid}")).json()["files"] == [f.model_dump()]
    assert await _rows(rid) == []


async def test_mixed_rollout_rows_of_either_mode_are_read_under_either_flag_value(flag):
    f = _file("out/a.png", PNG)
    ids = {}
    for mode in (True, False, True):                              # on -> off -> on
        flag(mode)
        key, rid = await _new_run()
        await append_event(rid, ev.RunCompleted(final_text="ok", files=[f]))
        ids.setdefault(mode, []).append((key, rid))
    for reading in (False, True):                                 # rollback = flag off: still readable
        flag(reading)
        for key, rid in ids[True] + ids[False]:
            assert (await _get(key, f"/{rid}")).json()["files"] == [f.model_dump()]
    assert [len(await _rows(r)) for _, r in ids[True] + ids[False]] == [1, 1, 0]


async def test_non_canonical_base64_stays_inline_and_is_served_verbatim(flag):
    f = ev.FileRef(path="x.png", size=1, content_b64="eA", expires_at=_iso(timedelta(hours=1)))
    key, rid = await _new_run()
    await append_event(rid, ev.RunCompleted(final_text="ok", files=[f]))
    assert (await _meta(rid))["files"] == [f.model_dump()] and await _rows(rid) == []
    assert (await _get(key, f"/{rid}")).json()["files"] == [f.model_dump()]


async def test_repeated_result_write_is_idempotent(flag):
    _, rid = await _new_run()
    f = _file("out/a.png", PNG)
    await append_event(rid, ev.RunCompleted(final_text="ok", files=[f]))
    with pytest.raises(RunConflict):                           # a second terminal event is rejected
        await append_event(rid, ev.RunCompleted(final_text="again", files=[_file("out/b.png", OTHER)]))
    assert [r.data for r in await _rows(rid)] == [PNG]
    for _ in range(2):                                         # and the store itself never doubles
        async with get_session() as s:
            await store_files(s, rid, [f.model_dump()], enabled=True)
    assert [(r.idx, r.data) for r in await _rows(rid)] == [(0, PNG)]


async def test_error_mid_write_leaves_no_orphan_rows_and_no_half_result(flag, monkeypatch):
    key, rid = await _new_run()
    import aibroker.agent.runs as runs_mod

    real_store = runs_mod.store_files

    async def store_then_fail(*a, **k):
        await real_store(*a, **k)
        raise RuntimeError("terminal write failed after the file insert")
    monkeypatch.setattr(runs_mod, "store_files", store_then_fail)
    with pytest.raises(RuntimeError):
        await append_event(rid, ev.RunCompleted(final_text="ok", files=[_file("out/a.png", PNG)]))
    assert await _rows(rid) == []
    meta = await _meta(rid)
    assert "files" not in (meta or {})
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        run = await s.get(AgentRunRow, rid)
    assert job.status != "done" and run.outcome is None
    monkeypatch.setattr(runs_mod, "store_files", real_store)  # the retry then succeeds once
    await append_event(rid, ev.RunCompleted(final_text="ok", files=[_file("out/a.png", PNG)]))
    assert [r.data for r in await _rows(rid)] == [PNG]
    assert (await _get(key, f"/{rid}")).json()["status"] == "completed"


async def test_deleting_the_job_removes_its_file_rows(flag):
    _, rid = await _new_run()
    await append_event(rid, ev.RunCompleted(final_text="ok", files=[_file("out/a.png", PNG)]))
    assert len(await _rows(rid)) == 1
    async with get_session() as s:
        if ON_SQLITE:
            await s.execute(text("PRAGMA foreign_keys=ON"))
        await s.execute(text("DELETE FROM deep_jobs WHERE id = :i"), {"i": rid})
    async with get_session() as s:
        assert (await s.execute(select(func.count()).select_from(DeepJobFileRow))).scalar_one() == 0


@pytest.mark.skipif(ON_SQLITE, reason="retention uses make_interval / DELETE...LIMIT: Postgres only")
async def test_retention_purge_removes_file_rows_with_the_job(flag):
    from aibroker.services.job_queue import JOB_RETENTION_DAYS, purge_finished_jobs

    _, rid = await _new_run()
    await append_event(rid, ev.RunCompleted(final_text="ok", files=[_file("out/a.png", PNG)]))
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
        job.completed_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=JOB_RETENTION_DAYS + 1)
    await purge_finished_jobs()
    assert await _rows(rid) == []


MIGRATION_025 = Path(__file__).resolve().parent.parent / "infra" / "sql" / "migrations" / "025_deep_job_files.sql"


async def _apply_migration_file() -> None:
    """The real file through asyncpg's simple-query protocol (as psql sends it, BEGIN/COMMIT included)."""
    async with engine_mod._engine.connect() as conn:
        raw = await conn.get_raw_connection()
        await raw.driver_connection.execute(MIGRATION_025.read_text(encoding="utf-8"))


async def _scalar(sql: str):
    async with get_session() as s:
        return (await s.execute(text(sql))).first()


@pytest.mark.skipif(ON_SQLITE, reason="applies the real SQL migration: Postgres only (CI integration job)")
async def test_real_migration_025_on_postgres_then_dual_read_and_retention_cascade(flag):
    """The ORM-built test schema minus deep_job_files stands in for the schema after migrations <= 024
    (the repo has no migration runner: migrations are applied by hand with psql)."""
    async with engine_mod._engine.begin() as conn:
        await conn.execute(text("SET LOCAL aibroker.allow_destructive_ddl = 'on'"))
        await conn.execute(text("DROP TABLE deep_job_files"))
    assert (await _scalar("SELECT to_regclass('deep_job_files')"))[0] is None

    await _apply_migration_file()
    await _apply_migration_file()                                  # idempotent (IF NOT EXISTS)

    assert (await _scalar("SELECT to_regclass('deep_job_files')::text"))[0] == "deep_job_files"
    assert (await _scalar("SELECT attstorage FROM pg_attribute WHERE attrelid = 'deep_job_files'::regclass "
                          "AND attname = 'data'"))[0] == "e"
    assert tuple(await _scalar("SELECT confdeltype, confrelid::regclass::text FROM pg_constraint "
                               "WHERE conrelid = 'deep_job_files'::regclass AND contype = 'f'")) == ("c", "deep_jobs")
    assert (await _scalar(
        "SELECT count(*) FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = i.indkey[0] WHERE i.indrelid = 'deep_job_files'::regclass "
        "AND a.attname = 'job_id'"))[0] >= 1                       # PK (job_id, idx) leads with job_id

    f = _file("out/a.png", PNG)
    new_key, new_id = await _new_run()                              # written after the migration, flag on
    await append_event(new_id, ev.RunCompleted(final_text="ok", files=[f]))
    old_key, old_id = await _legacy_run([f])                        # legacy inline row
    assert len(await _rows(new_id)) == 1 and await _rows(old_id) == []
    assert (await _get(new_key, f"/{new_id}")).json()["files"] == [f.model_dump()]
    assert (await _get(old_key, f"/{old_id}")).json()["files"] == [f.model_dump()]

    from aibroker.services.job_queue import JOB_RETENTION_DAYS, purge_finished_jobs

    async with get_session() as s:
        job = await s.get(DeepJobRow, new_id)
        job.completed_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=JOB_RETENTION_DAYS + 1)
    assert await purge_finished_jobs() >= 1
    assert await _rows(new_id) == []
