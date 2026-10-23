"""aibroker.scripts.clear_expired_files: clears only the CONTENT of already-expired inline agent
files (jsonb rewrite in SQL, Postgres only: the CI integration job runs it for real)."""
from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from test_agent_job_files import PNG, _file, _legacy_run
from test_agent_runs import _get, _project

from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.job_files import store_files
from aibroker.db import get_session
from aibroker.db.models import DeepJobFileRow, DeepJobRow
from aibroker.routes import proxy
from aibroker.scripts import clear_expired_files as tool

pytestmark = pytest.mark.skipif("sqlite" in os.environ.get("DATABASE_URL", ""),
                                reason="jsonb rewrite + row locks: Postgres only (CI integration job)")

CUTOFF = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
B64 = base64.b64encode(PNG).decode()


def _f(path: str, expires: datetime | str | None, **extra) -> dict:
    out = {"path": path, "size": len(PNG), "sha256": "ab" * 32, "content_b64": B64}
    if expires is not None:
        out["expires_at"] = expires if isinstance(expires, str) else expires.isoformat()
    return {**out, **extra}


async def _job(files: list | None, *, status: str = "done", cap: str = "agent:claude",
               extra_meta: dict | None = None) -> tuple[int, int]:
    _, pid = await _project([AGENT_SCOPE])
    meta = {**(extra_meta or {})}
    if files is not None:
        meta["files"] = files
    async with get_session() as s:
        row = DeepJobRow(project_id=pid, capability=cap, status=status, request={"p": 1},
                         result_text="ok", result_meta=meta,
                         completed_at=datetime.now(UTC).replace(tzinfo=None))
        s.add(row)
        await s.flush()
        return row.id, pid


async def _meta(jid: int) -> dict:
    async with get_session() as s:
        return (await s.get(DeepJobRow, jid)).result_meta


def _cleared(f: dict) -> dict:
    return {**f, "content_b64": None, "expired": True}


async def test_expiry_boundary_is_strict_and_to_the_microsecond():
    before = _f("a.png", CUTOFF - timedelta(seconds=1))
    micro = _f("b.png", CUTOFF - timedelta(microseconds=1))
    exact = _f("c.png", CUTOFF)
    after = _f("d.png", CUTOFF + timedelta(seconds=1))
    frac = _f("e.png", CUTOFF + timedelta(microseconds=500000))
    jid, _ = await _job([before, micro, exact, after, frac])
    rep = await tool.run(CUTOFF, execute=True)
    assert (await _meta(jid))["files"] == [_cleared(before), _cleared(micro), exact, after, frac]
    assert (rep.jobs_updated, rep.files_cleared, rep.bytes_cleared) == (1, 2, 2 * len(B64))
    assert rep.skipped_live == 3


async def test_missing_or_bad_expires_at_is_skipped_and_reported():
    files = [_f("a.png", None), _f("b.png", ""), _f("c.png", "not-a-date"), _f("d.png", None, expires_at=None)]
    jid, _ = await _job(files)
    rep = await tool.run(CUTOFF, execute=True)
    assert (await _meta(jid))["files"] == files
    assert rep.skipped_no_expires_at == 4 and rep.files_cleared == 0 and rep.jobs_updated == 0


async def test_second_run_is_a_no_op():
    jid, _ = await _job([_f("a.png", CUTOFF - timedelta(hours=1))])
    first = await tool.run(CUTOFF, execute=True)
    snapshot = await _meta(jid)
    second = await tool.run(CUTOFF, execute=True)
    assert first.files_cleared == 1 and second.files_cleared == 0 and second.jobs_updated == 0
    assert second.bytes_cleared == 0 and await _meta(jid) == snapshot


async def test_everything_else_is_preserved_exactly():
    old = _f("old.png", CUTOFF - timedelta(days=1), width=3, height=4, image_call_id="c1")
    live = _f("live.png", CUTOFF + timedelta(hours=1), width=1)
    skipped = {"path": "big.bin", "size": 9, "skipped": "too_large"}
    jid, _ = await _job([live, skipped, old, "odd", 7],
                        extra_meta={"usage": {"input": 5}, "model_observations": [{"model": "m"}], "z": None})
    await tool.run(CUTOFF, execute=True)
    meta = await _meta(jid)
    assert meta["files"] == [live, skipped, _cleared(old), "odd", 7]          # order and every key kept
    assert {k: v for k, v in meta.items() if k != "files"} == {
        "usage": {"input": 5}, "model_observations": [{"model": "m"}], "z": None}
    async with get_session() as s:                                            # other columns untouched
        row = await s.get(DeepJobRow, jid)
    assert (row.request, row.result_text, row.status) == ({"p": 1}, "ok", "done")


async def test_scope_only_terminal_agent_jobs():
    old = _f("a.png", CUTOFF - timedelta(hours=1))
    running, _ = await _job([old], status="running")
    chat, _ = await _job([old], cap="chat:deep")
    nofiles, _ = await _job(None)
    rep = await tool.run(CUTOFF, execute=True)
    assert (await _meta(running))["files"] == [old] and (await _meta(chat))["files"] == [old]
    assert "files" not in await _meta(nofiles) and rep.files_cleared == 0


async def test_dry_run_writes_nothing_and_matches_execute():
    jid, pid = await _job([_f("a.png", CUTOFF - timedelta(hours=1)), _f("b.png", CUTOFF - timedelta(hours=2))])
    before = await _meta(jid)
    async with get_session() as s:
        xmin = (await s.execute(text("SELECT xmin::text FROM deep_jobs WHERE id = :i"), {"i": jid})).scalar_one()
    dry = await tool.run(CUTOFF, execute=False)
    async with get_session() as s:
        assert (await s.execute(text("SELECT xmin::text FROM deep_jobs WHERE id = :i"),
                                {"i": jid})).scalar_one() == xmin          # row not even rewritten
    assert await _meta(jid) == before
    assert (dry.jobs_clearable, dry.files_clearable, dry.bytes_clearable) == (1, 2, 2 * len(B64))
    assert dry.by_project_capability[(pid, "agent:claude")] == [1, 2, 2 * len(B64)]
    assert dry.jobs_updated == dry.files_cleared == 0
    real = await tool.run(CUTOFF, execute=True)
    assert (real.jobs_updated, real.files_cleared, real.bytes_cleared) == (1, 2, dry.bytes_clearable)


async def test_batches_cover_every_job():
    ids = [(await _job([_f("a.png", CUTOFF - timedelta(hours=1))]))[0] for _ in range(5)]
    rep = await tool.run(CUTOFF, execute=True, batch_size=2)
    assert rep.jobs_updated == 5
    for i in ids:
        assert (await _meta(i))["files"][0]["expired"] is True


async def test_locked_row_is_skipped_then_picked_up_next_run():
    jid, _ = await _job([_f("a.png", CUTOFF - timedelta(hours=1))])
    other, _ = await _job([_f("a.png", CUTOFF - timedelta(hours=1))])
    async with get_session() as writer:                                       # a concurrent writer
        await writer.execute(select(DeepJobRow).where(DeepJobRow.id == jid).with_for_update())
        rep = await tool.run(CUTOFF, execute=True)
        assert rep.jobs_locked_skipped == 1 and rep.jobs_updated == 1
        assert (await _meta(jid))["files"][0]["content_b64"] == B64          # untouched while locked
    again = await tool.run(CUTOFF, execute=True)
    assert again.jobs_updated == 1 and (await _meta(jid))["files"][0]["content_b64"] is None
    assert (await _meta(other))["files"][0]["expired"] is True


async def test_api_json_is_identical_before_and_after_for_expired_and_live_files():
    files = [_file("old.png", PNG, expires=timedelta(hours=-1)), _file("live.png", PNG)]
    key, rid = await _legacy_run(files)
    async with get_session() as s:
        row = await s.get(DeepJobRow, rid)
    jobs_before = proxy._job_response(row).files
    runs_before = (await _get(key, f"/{rid}")).json()["files"]
    rep = await tool.run(tool.default_cutoff(), execute=True)
    assert rep.files_cleared == 1
    async with get_session() as s:
        row = await s.get(DeepJobRow, rid)
    assert row.result_meta["files"][0]["content_b64"] is None
    assert proxy._job_response(row).files == jobs_before
    after = (await _get(key, f"/{rid}")).json()["files"]
    assert after == runs_before
    assert after[0]["content_b64"] is None and after[0]["expired"] is True
    assert base64.b64decode(after[1]["content_b64"]) == PNG                   # live file untouched
    assert json.dumps(after, sort_keys=True) == json.dumps(runs_before, sort_keys=True)


async def test_deep_job_files_rows_are_reported_but_never_changed():
    key, rid = await _legacy_run([])
    async with get_session() as s:
        stored = await store_files(s, rid, [_file("t.png", PNG, expires=timedelta(hours=-2)).model_dump()],
                                   enabled=True)
        job = await s.get(DeepJobRow, rid)
        job.result_meta = {"files": stored}
    cols = (DeepJobFileRow.job_id, DeepJobFileRow.idx, DeepJobFileRow.data, DeepJobFileRow.size)
    async with get_session() as s:
        before = (await s.execute(select(*cols))).all()
    rep = await tool.run(tool.default_cutoff(), execute=True)
    async with get_session() as s:
        after = (await s.execute(select(*cols))).all()
    assert after == before and len(after) == 1
    assert (rep.table_rows, rep.table_bytes, rep.table_rows_expired) == (1, len(PNG), 1)
    assert rep.files_table_ref == 1 and rep.files_cleared == 0
    assert (await _get(key, f"/{rid}")).json()["files"][0]["expired"] is True


def _args(*argv: str):
    return tool.build_parser().parse_args(list(argv))


def test_execute_needs_exact_confirm_and_explicit_safe_cutoff():
    iso = CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ")
    now = datetime.now(UTC)
    assert tool.resolve(_args(), now)[1] is False                              # dry-run by default
    ok = _args("--execute", "--cutoff", iso, "--confirm", f"CLEAR-EXPIRED-{iso}")
    assert tool.resolve(ok, now) == (CUTOFF, True)
    future = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bad in (["--execute", "--cutoff", iso],
                ["--execute", "--cutoff", iso, "--confirm", "yes"],
                ["--execute", "--cutoff", iso, "--confirm", "CLEAR-EXPIRED-2020-01-01T00:00:00Z"],
                ["--execute", "--confirm", f"CLEAR-EXPIRED-{iso}"],              # no explicit cutoff
                ["--cutoff", iso, "--confirm", f"CLEAR-EXPIRED-{iso}"],          # confirm without execute
                ["--cutoff", future],                                            # future cutoff
                ["--cutoff", "2026-10-01T00:00:00"]):                            # no UTC offset
        with pytest.raises(ValueError):
            tool.resolve(_args(*bad), now)
    default = tool.default_cutoff(now)
    assert default == (now - timedelta(minutes=5)).replace(microsecond=0) and default.microsecond == 0


async def test_main_refuses_without_confirm_and_changes_nothing():
    jid, _ = await _job([_f("a.png", CUTOFF - timedelta(hours=1))])
    iso = CUTOFF.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert await tool.main(["--execute", "--cutoff", iso]) == 2
    assert (await _meta(jid))["files"][0]["content_b64"] == B64
