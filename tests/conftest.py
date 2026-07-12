"""Test fixtures.

Default: in-memory SQLite (fast, no deps) — used for everything except the
Postgres-only selector tests, which `skipif` themselves off SQLite.

CI integration job sets DATABASE_URL to a real Postgres; then this fixture
binds the engine to it and the Postgres-only tests run for real.
"""
from __future__ import annotations

import os
import tempfile

# Test-time defaults for env-driven settings (BEFORE any aibroker import).
# Default DATABASE_URL to SQLite so a bare `pytest` doesn't run the Postgres-only
# tests against SQLite (their ON_SQLITE/skipif guards read this var). CI overrides
# it with a real Postgres URL to exercise those tests.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-for-prod")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
# The notifier's throttle-state dir defaults to /var/lib/aibroker — not
# writable for the CI runner user, and monitor.tick() now drives the real
# notifier (paid-tail check) in the Postgres tests that don't patch it.
os.environ.setdefault("ALERT_STATE_DIR", tempfile.mkdtemp(prefix="aib-alert-state-"))

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import aibroker.db.engine as engine_mod
from aibroker.db.engine import Base

_DB_URL = os.environ["DATABASE_URL"]
_IS_PG = "postgres" in _DB_URL or "asyncpg" in _DB_URL


@pytest_asyncio.fixture(autouse=True)
async def db():
    """Fresh schema per test. Postgres when DATABASE_URL targets it, else SQLite.

    On Postgres we use NullPool: the sync Starlette TestClient runs requests in
    its own event-loop portal, and a pooled asyncpg connection bound to the
    fixture's loop can't be reused there. NullPool opens a fresh connection in
    whatever loop is current, avoiding 'another operation is in progress'.
    """
    if _IS_PG:
        e = create_async_engine(_DB_URL, poolclass=NullPool)
    else:
        e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        if _IS_PG:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._sessionmaker = async_sessionmaker(e, expire_on_commit=False)
    yield
    if _IS_PG:
        async with e.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    await e.dispose()
    engine_mod._engine = None
    engine_mod._sessionmaker = None
