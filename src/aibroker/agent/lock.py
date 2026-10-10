"""Exclusive per-(account_id, session_id) lock: two runs never drive one CLI session.

Postgres: a session-level advisory lock on a dedicated DIRECT connection (never the
PgBouncer pool - transaction pooling would drop a session lock). Elsewhere (SQLite/tests):
an in-process set.
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import asyncpg

from aibroker.config import get_settings
from aibroker.services.job_queue import _dialect_name, _listen_dsn

_held: set[str] = set()


def lock_name(account_id: str, session_id: str) -> str:
    return f"agent-session:{account_id}:{session_id}"


@contextlib.asynccontextmanager
async def session_lock(account_id: str, session_id: str) -> AsyncIterator[bool]:
    """Yields True when the lock is held for the block, False when someone else has it."""
    name = lock_name(account_id, session_id)
    if await _dialect_name() != "postgresql":
        if name in _held:
            yield False
            return
        _held.add(name)
        try:
            yield True
        finally:
            _held.discard(name)
        return
    conn = await asyncpg.connect(_listen_dsn(get_settings().direct_database_url))  # pragma: no cover
    try:  # pragma: no cover - Postgres only, covered by the PG lock-contention test
        got = await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", name)
        try:
            yield bool(got)
        finally:
            if got:
                await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", name)
    finally:
        await conn.close()
