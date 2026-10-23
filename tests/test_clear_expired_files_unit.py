"""aibroker.scripts.clear_expired_files: logic that does not need Postgres (runs on the SQLite
quality job): argument validation, confirm token, report rendering and the dry-run / execute /
locked / failed-batch orchestration against a scripted fake session.

The jsonb SQL itself (selection, rewrite, row locks) is exercised for real by
test_agent_clear_expired_files.py in the CI integration job (Postgres); here every statement is
only recorded, never executed, so these tests pin WHICH statements run, in which order, and how
their results are folded into the report."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError

from aibroker.scripts import clear_expired_files as tool

CUTOFF = datetime(2026, 10, 23, 10, 0, 0, tzinfo=UTC)
ISO = "2026-10-23T10:00:00Z"
NOW = datetime(2026, 10, 23, 12, 0, 0, tzinfo=UTC)
SET_RO = "SET TRANSACTION READ ONLY"
SET_LOCK = "SET LOCAL lock_timeout = '2s'"
SET_STMT = "SET LOCAL statement_timeout = '30s'"
TABLE_ROW = [(6, 60, 2, 20, 1)]


def _args(*argv: str):
    return tool.build_parser().parse_args(list(argv))


# ---------------------------------------------------------------- arguments / pure helpers

def test_execute_needs_exact_confirm_and_explicit_safe_cutoff():
    assert tool.resolve(_args(), NOW)[1] is False                              # dry-run by default
    ok = _args("--execute", "--cutoff", ISO, "--confirm", f"CLEAR-EXPIRED-{ISO}")
    assert tool.resolve(ok, NOW) == (CUTOFF, True)
    future = (NOW + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for bad in (["--execute", "--cutoff", ISO],
                ["--execute", "--cutoff", ISO, "--confirm", "yes"],
                ["--execute", "--cutoff", ISO, "--confirm", "CLEAR-EXPIRED-2020-01-01T00:00:00Z"],
                ["--execute", "--confirm", f"CLEAR-EXPIRED-{ISO}"],              # no explicit cutoff
                ["--cutoff", ISO, "--confirm", f"CLEAR-EXPIRED-{ISO}"],          # confirm without execute
                ["--cutoff", future],                                            # future cutoff
                ["--cutoff", "2026-10-01T00:00:00"],                             # no UTC offset
                ["--cutoff", "2026-10-01T00:00:00.5Z"],                          # sub-second
                ["--batch-size", "0"],
                ["--margin-seconds", "-1"]):
        with pytest.raises(ValueError):
            tool.resolve(_args(*bad), NOW)


def test_default_cutoff_is_now_minus_margin_in_whole_seconds_utc():
    now = datetime(2026, 10, 23, 12, 0, 7, 123456, tzinfo=UTC)
    assert tool.default_cutoff(now) == datetime(2026, 10, 23, 11, 55, 7, tzinfo=UTC)
    assert tool.default_cutoff(now, 0) == datetime(2026, 10, 23, 12, 0, 7, tzinfo=UTC)
    assert tool.default_cutoff().tzinfo is not None
    assert tool.resolve(_args("--margin-seconds", "60"), NOW)[0] == NOW - timedelta(seconds=60)


def test_confirm_token_and_cutoff_roundtrip_with_offsets():
    assert tool.confirm_token(CUTOFF) == f"CLEAR-EXPIRED-{ISO}"
    plus3 = tool.parse_cutoff("2026-10-23T13:00:00+03:00")
    assert plus3 == CUTOFF and plus3.utcoffset() == timedelta(0)
    assert tool.confirm_token(datetime.fromisoformat("2026-10-23T13:00:00+03:00")) == f"CLEAR-EXPIRED-{ISO}"


def test_params_use_utc_isoformat_and_agent_scope():
    p = tool._params(datetime.fromisoformat("2026-10-23T13:00:00+03:00"))
    assert p == {"cutoff": "2026-10-23T10:00:00+00:00", "cap_like": "agent:%"}


def test_report_lines_dry_run_and_execute():
    rep = tool.Report(cutoff=CUTOFF, execute=False, jobs_scanned=3, jobs_clearable=1,
                      files_clearable=2, bytes_clearable=40, skipped_live=5,
                      skipped_no_expires_at=1, files_table_ref=4, table_rows=6, table_bytes=60,
                      table_rows_expired=2, table_bytes_expired=20, table_rows_no_expires_at=1)
    rep.by_project_capability[(7, "agent:claude")] = [1, 2, 40]
    rep.by_project_capability[(2, "agent:codex")] = [1, 1, 5]
    dry = "\n".join(rep.lines())
    assert "DRY-RUN (nothing written)" in dry and "jobs updated" not in dry
    assert "cutoff (UTC): 2026-10-23T10:00:00+00:00" in dry
    assert "files to clear: 2" in dry and "base64 bytes to clear: 40" in dry
    assert "skipped, not expired at cutoff (inline): 5" in dry
    assert "skipped, no/invalid expires_at (inline): 1" in dry
    assert "files already in deep_job_files (not touched): 4" in dry
    assert dry.index("project 2 agent:codex") < dry.index("project 7 agent:claude")    # sorted
    assert "project 7 agent:claude: 1 jobs, 2 files, 40 bytes" in dry
    assert "rows: 6, sum(size): 60" in dry
    assert "expired at cutoff: 2 rows, sum(size): 20" in dry and "without expires_at: 1" in dry
    rep.execute, rep.jobs_updated, rep.files_cleared, rep.bytes_cleared = True, 1, 2, 40
    rep.jobs_locked_skipped, rep.batches_failed = 3, 1
    real = "\n".join(rep.lines())
    assert "mode: EXECUTE" in real and "jobs updated: 1" in real and "files cleared: 2" in real
    assert "base64 bytes cleared: 40" in real
    assert "jobs skipped (row locked by a writer, next run): 3" in real
    assert "batches failed (lock/statement timeout): 1" in real


def test_parser_defaults_and_mutually_exclusive_modes():
    a = _args()
    assert (a.cutoff, a.batch_size, a.margin_seconds, a.execute, a.dry_run, a.confirm) == (
        None, tool.DEFAULT_BATCH, tool.DEFAULT_MARGIN_S, False, False, None)
    with pytest.raises(SystemExit):
        _args("--execute", "--dry-run")


# ---------------------------------------------------------------- scripted fake session

class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def one(self):
        return self._rows[0]

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class _FakeDb:
    """Stands in for get_session(): records every statement and answers the tool's named SQL
    objects from `answers`. An answer is a list of rows (reused on every call), an exception
    (raised), or `_Seq` (one answer per call, in order)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.log: list[tuple[str, dict | None]] = []
        self.sessions = 0

    @staticmethod
    def label(stmt) -> str:
        for name in ("_IDS_SQL", "_SURVEY_SQL", "_LOCK_SQL", "_CLEAR_SQL", "_TABLE_SQL"):
            if stmt is getattr(tool, name):
                return name
        return str(stmt)

    async def execute(self, stmt, params=None):
        label = self.label(stmt)
        self.log.append((label, params))
        ans = self.answers.get(label, [])
        if isinstance(ans, _Seq):
            ans = ans.items.pop(0)
        if isinstance(ans, BaseException):
            raise ans
        return _Result(ans)

    @asynccontextmanager
    async def session(self):
        self.sessions += 1
        yield self

    def labels(self) -> list[str]:
        return [lab for lab, _ in self.log]


class _Seq:
    def __init__(self, *items):
        self.items = list(items)


@pytest.fixture
def fake_db(monkeypatch):
    def install(**answers):
        db = _FakeDb(answers)
        monkeypatch.setattr(tool, "get_session", db.session)
        return db
    return install


def _timeout() -> DBAPIError:
    return DBAPIError("stmt", {}, Exception("canceling statement due to statement timeout"))


async def test_begin_read_only_is_the_first_statement_and_sets_timeouts(fake_db):
    db = fake_db()
    async with tool.get_session() as s:
        await tool._begin(s, read_only=True)
        await tool._begin(s, read_only=False)
    assert db.labels() == [SET_RO, SET_LOCK, SET_STMT, SET_LOCK, SET_STMT]


async def test_table_report_reads_counts_with_a_naive_utc_cutoff(fake_db):
    db = fake_db(_TABLE_SQL=TABLE_ROW)
    rep = tool.Report(cutoff=CUTOFF, execute=False)
    await tool.table_report(CUTOFF, rep)
    assert (rep.table_rows, rep.table_bytes, rep.table_rows_expired, rep.table_bytes_expired,
            rep.table_rows_no_expires_at) == (6, 60, 2, 20, 1)
    assert db.log[0][0] == SET_RO
    assert db.log[-1] == ("_TABLE_SQL", {"cutoff_naive": datetime(2026, 10, 23, 10, 0, 0)})


async def test_dry_run_surveys_in_batches_and_never_locks_or_writes(fake_db):
    survey_1 = [(1, 7, "agent:claude", 2, 40, 1, 0, 1), (2, 7, "agent:claude", 0, 0, 3, 2, 0)]
    survey_2 = [(3, 9, "agent:codex", 1, 5, 0, 1, 0)]
    db = fake_db(_IDS_SQL=[(1,), (2,), (3,)], _SURVEY_SQL=_Seq(survey_1, survey_2), _TABLE_SQL=TABLE_ROW)
    rep = await tool.run(CUTOFF, execute=False, batch_size=2)
    assert rep.jobs_scanned == 3 and rep.jobs_clearable == 2
    assert (rep.files_clearable, rep.bytes_clearable) == (3, 45)
    assert (rep.skipped_live, rep.skipped_no_expires_at, rep.files_table_ref) == (4, 3, 1)
    assert dict(rep.by_project_capability) == {(7, "agent:claude"): [1, 2, 40], (9, "agent:codex"): [1, 1, 5]}
    assert rep.jobs_updated == rep.files_cleared == rep.bytes_cleared == rep.batches_failed == 0
    assert "_LOCK_SQL" not in db.labels() and "_CLEAR_SQL" not in db.labels()
    surveys = [p for lab, p in db.log if lab == "_SURVEY_SQL"]
    assert [p["ids"] for p in surveys] == [[1, 2], [3]]                       # batch_size honoured
    assert all(p["cutoff"] == "2026-10-23T10:00:00+00:00" and p["cap_like"] == "agent:%" for p in surveys)
    # every dry-run transaction is read-only: ids scan + 2 batches + table report
    assert db.labels().count(SET_RO) == db.sessions == 4
    assert rep.table_rows == 6


async def test_execute_locks_only_clearable_jobs_and_clears_what_it_locked(fake_db):
    survey = [(1, 7, "agent:claude", 2, 40, 0, 0, 0), (2, 7, "agent:claude", 0, 0, 1, 0, 0),
              (3, 7, "agent:claude", 1, 10, 0, 0, 0)]
    db = fake_db(_IDS_SQL=[(1,), (2,), (3,)], _SURVEY_SQL=survey, _LOCK_SQL=[(1,)],
                 _CLEAR_SQL=[(1, 2, 40)], _TABLE_SQL=TABLE_ROW)
    rep = await tool.run(CUTOFF, execute=True)
    lock = next(p for lab, p in db.log if lab == "_LOCK_SQL")
    clear = next(p for lab, p in db.log if lab == "_CLEAR_SQL")
    assert lock == {"ids": [1, 3]}                                           # job 2 has nothing to clear
    assert clear["ids"] == [1] and clear["cutoff"] == "2026-10-23T10:00:00+00:00"
    assert (rep.jobs_clearable, rep.files_clearable, rep.bytes_clearable) == (2, 3, 50)
    assert rep.jobs_locked_skipped == 1                                      # job 3 was held by a writer
    assert (rep.jobs_updated, rep.files_cleared, rep.bytes_cleared) == (1, 2, 40)
    labels = db.labels()
    assert labels[0] == SET_RO                                               # ids scan is read-only
    start = labels.index("_SURVEY_SQL")
    assert labels[start - 2:start] == [SET_LOCK, SET_STMT]                   # the write batch is not
    assert labels[start - 3] != SET_RO
    assert labels.index("_LOCK_SQL") < labels.index("_CLEAR_SQL")


async def test_execute_with_every_clearable_job_locked_writes_nothing(fake_db):
    db = fake_db(_IDS_SQL=[(1,)], _SURVEY_SQL=[(1, 7, "agent:claude", 1, 10, 0, 0, 0)],
                 _LOCK_SQL=[], _TABLE_SQL=TABLE_ROW)
    rep = await tool.run(CUTOFF, execute=True)
    assert rep.jobs_locked_skipped == 1 and rep.jobs_updated == 0 and rep.files_cleared == 0
    assert "_CLEAR_SQL" not in db.labels()


async def test_execute_with_nothing_clearable_does_not_even_lock(fake_db):
    db = fake_db(_IDS_SQL=[(1,)], _SURVEY_SQL=[(1, 7, "agent:claude", 0, 0, 2, 1, 0)], _TABLE_SQL=TABLE_ROW)
    rep = await tool.run(CUTOFF, execute=True)
    assert (rep.skipped_live, rep.skipped_no_expires_at, rep.jobs_clearable) == (2, 1, 0)
    assert "_LOCK_SQL" not in db.labels() and "_CLEAR_SQL" not in db.labels()


async def test_failed_batch_is_counted_and_later_batches_still_run(fake_db):
    db = fake_db(_IDS_SQL=[(1,), (2,)],
                 _SURVEY_SQL=_Seq(_timeout(), [(2, 7, "agent:claude", 1, 10, 0, 0, 0)]),
                 _LOCK_SQL=[(2,)], _CLEAR_SQL=[(2, 1, 10)], _TABLE_SQL=TABLE_ROW)
    rep = await tool.run(CUTOFF, execute=True, batch_size=1)
    assert rep.batches_failed == 1 and rep.jobs_updated == 1 and rep.files_cleared == 1
    assert db.labels().count("_SURVEY_SQL") == 2 and db.labels()[-1] == "_TABLE_SQL"


async def test_run_with_no_candidates_still_reports_the_table(fake_db):
    db = fake_db(_IDS_SQL=[], _TABLE_SQL=TABLE_ROW)
    rep = await tool.run(CUTOFF, execute=False)
    assert rep.jobs_scanned == 0 and rep.table_rows == 6 and "_SURVEY_SQL" not in db.labels()


# ---------------------------------------------------------------- main()

@pytest.fixture
def engine_calls(monkeypatch):
    calls: list[str] = []

    async def init():
        calls.append("init")

    async def close():
        calls.append("close")

    monkeypatch.setattr(tool, "init_engine", init)
    monkeypatch.setattr(tool, "close_engine", close)
    return calls


async def test_main_refuses_without_confirm_before_touching_the_database(engine_calls, capsys):
    iso = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert await tool.main(["--execute", "--cutoff", iso]) == 2
    assert engine_calls == []                                                 # engine never opened
    assert "refusing to execute" in capsys.readouterr().err


async def test_main_dry_run_returns_zero_and_closes_the_engine(fake_db, engine_calls):
    db = fake_db(_IDS_SQL=[], _TABLE_SQL=TABLE_ROW)
    assert await tool.main(["--cutoff", ISO]) == 0
    assert engine_calls == ["init", "close"] and "_CLEAR_SQL" not in db.labels()


async def test_main_returns_one_when_a_batch_failed_and_still_closes_the_engine(fake_db, engine_calls):
    fake_db(_IDS_SQL=[(1,)], _SURVEY_SQL=_timeout(), _TABLE_SQL=TABLE_ROW)
    assert await tool.main(["--cutoff", ISO]) == 1
    assert engine_calls == ["init", "close"]


async def test_main_closes_the_engine_when_run_raises(fake_db, engine_calls):
    fake_db(_IDS_SQL=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await tool.main(["--cutoff", ISO])
    assert engine_calls == ["init", "close"]
