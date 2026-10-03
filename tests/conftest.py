"""Test fixtures.

Default: in-memory SQLite (fast, no deps) — used for everything except the
Postgres-only selector tests, which `skipif` themselves off SQLite.

CI integration job sets TEST_DATABASE_URL (db name ending `_test`) to a real Postgres; then this fixture
binds the engine to it and the Postgres-only tests run for real.
"""
from __future__ import annotations

import os
import tempfile

# Tests NEVER read DATABASE_URL / DIRECT_DATABASE_URL from the environment: on
# 2026-10-03 pytest ran in a prod container, picked up the prod DATABASE_URL and
# the `db` fixture's drop_all wiped every table. Only TEST_DATABASE_URL counts
# (default: in-memory SQLite) and it is forced into the vars the app reads.
from sqlalchemy.engine import make_url


def _assert_safe_test_db_url(url: str) -> None:
    """Raise ValueError unless `url` is SQLite or a Postgres db named *_test."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        return
    if parsed.get_backend_name() == "postgresql":
        name = parsed.database or ""
        if name.endswith("_test"):
            return
        raise ValueError(
            f"refusing to run tests against Postgres database {name!r}: "
            "TEST_DATABASE_URL must name a database ending in '_test'"
        )
    raise ValueError(f"unsupported TEST_DATABASE_URL backend: {parsed.get_backend_name()!r}")


_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
try:
    _assert_safe_test_db_url(_TEST_DB_URL)
except ValueError as _exc:
    import pytest as _pytest

    _pytest.exit(f"tests/conftest.py: {_exc}. Never run pytest in a prod container.", returncode=2)

os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["DIRECT_DATABASE_URL"] = _TEST_DB_URL
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-for-prod")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")
# The notifier's throttle-state dir defaults to /var/lib/aibroker — not
# writable for the CI runner user, and monitor.tick() now drives the real
# notifier (paid-tail check) in the Postgres tests that don't patch it.
os.environ.setdefault("ALERT_STATE_DIR", tempfile.mkdtemp(prefix="aib-alert-state-"))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import aibroker.db.engine as engine_mod
from aibroker.db.engine import Base

_DB_URL = _TEST_DB_URL
_IS_PG = "postgres" in _DB_URL or "asyncpg" in _DB_URL


async def _allow_ddl(conn) -> None:
    """Lift the migration-012 DROP guard for this (already `_test`-checked) connection."""
    from sqlalchemy import text

    await conn.execute(text("SET LOCAL aibroker.allow_destructive_ddl = 'on'"))


@pytest.fixture(autouse=True)
def _reset_circuit():
    """The timeout circuit-breaker is per-process module state — clear it around
    every test so a timeout noted in one test can't soft-skip a provider in the
    next (would flake the Postgres selector picks)."""
    from aibroker.routing import circuit
    circuit.reset()
    yield
    circuit.reset()


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
            await _allow_ddl(conn)
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._sessionmaker = async_sessionmaker(e, expire_on_commit=False)
    yield
    if _IS_PG:
        async with e.begin() as conn:
            await _allow_ddl(conn)
            await conn.run_sync(Base.metadata.drop_all)
    await e.dispose()
    engine_mod._engine = None
    engine_mod._sessionmaker = None
