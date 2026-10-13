"""pgbouncer + prepared statements: star projections are banned, and a stale
cached statement (InvalidCachedStatementError) is retried exactly once."""
from __future__ import annotations

import logging
import re
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.dialects.postgresql.asyncpg import AsyncAdapt_asyncpg_dbapi
from sqlalchemy.exc import DBAPIError, IntegrityError

from aibroker.db.resilience import is_stale_statement_error, retry_stale_statement

SRC = Path(__file__).resolve().parent.parent / "src" / "aibroker"
# `SELECT * FROM r2` reads a CTE whose columns are listed explicitly in the same
# statement (routes/dashboard_requests.py), so its result type cannot drift.
ALLOWLIST = {"routes/dashboard_requests.py"}
STAR = re.compile(r"RETURNING\s+\*|SELECT\s+\*", re.IGNORECASE)


def test_no_star_projection_in_raw_sql():
    offenders = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWLIST:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if STAR.search(line):
                offenders.append(f"{rel}:{n}: {line.strip()}")
    assert not offenders, "star projections break after ALTER TABLE ADD COLUMN:\n" + "\n".join(offenders)


def _stale() -> DBAPIError:
    orig = AsyncAdapt_asyncpg_dbapi(asyncpg).InvalidCachedStatementError("plan changed")
    return DBAPIError("SELECT 1", None, orig)


def test_matcher_is_exact():
    assert is_stale_statement_error(_stale())
    other = AsyncAdapt_asyncpg_dbapi(asyncpg).IntegrityError("dup")
    assert not is_stale_statement_error(IntegrityError("x", None, other))
    assert not is_stale_statement_error(ValueError("x"))


async def test_retry_once_then_success(caplog):
    calls = []

    @retry_stale_statement
    async def op():
        calls.append(1)
        if len(calls) == 1:
            raise _stale()
        return "ok"

    with caplog.at_level(logging.WARNING):
        assert await op() == "ok"
    assert len(calls) == 2
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_second_failure_propagates():
    calls = []

    @retry_stale_statement
    async def op():
        calls.append(1)
        raise _stale()

    with pytest.raises(DBAPIError):
        await op()
    assert len(calls) == 2


async def test_other_errors_not_retried():
    calls = []

    @retry_stale_statement
    async def op():
        calls.append(1)
        raise IntegrityError("x", None, AsyncAdapt_asyncpg_dbapi(asyncpg).IntegrityError("d"))

    with pytest.raises(IntegrityError):
        await op()
    assert len(calls) == 1
