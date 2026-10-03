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
from _isolation import (
    assert_connected_db_is_test,
    assert_safe_test_db_url,
    isolate_env,
    sandbox_violation,
)

_violation = sandbox_violation(os.environ, cwd=os.getcwd(), exists=os.path.exists)
if _violation:
    import pytest as _pytest

    _pytest.exit(
        f"tests/conftest.py: {_violation}. Never run pytest in a prod container "
        "(set AIB_TEST_SANDBOX=1 for a deliberate isolated test container).",
        returncode=2,
    )

_assert_safe_test_db_url = assert_safe_test_db_url  # name kept for test_db_url_guard
_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
try:
    assert_safe_test_db_url(_TEST_DB_URL)
except ValueError as _exc:
    import pytest as _pytest

    _pytest.exit(f"tests/conftest.py: {_exc}. Never run pytest in a prod container.", returncode=2)

os.environ["DATABASE_URL"] = _TEST_DB_URL
os.environ["DIRECT_DATABASE_URL"] = _TEST_DB_URL
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-for-prod")
# Fresh throttle-state dir (default /var/lib/aibroker is prod's, and unwritable
# for the CI user) and every externally-reaching setting forced inert.
isolate_env(os.environ, tempfile.mkdtemp(prefix="aib-alert-state-"))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import aibroker.config as config_mod
import aibroker.db.engine as engine_mod
from aibroker.db.engine import Base

# Never read a cwd .env (it may hold prod keys): tests get env vars only.
config_mod.Settings.model_config["env_file"] = None

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


@pytest.fixture(autouse=True)
def _reset_affinity():
    """Cache-affinity pins are per-process module state — a pin noted in one test
    must not steer the walk of the next (the DB reuses key ids per test)."""
    from aibroker.routing import affinity
    affinity.reset()
    yield
    affinity.reset()


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
            from sqlalchemy import text

            assert_connected_db_is_test(
                (await conn.execute(text("SELECT current_database()"))).scalar_one()
            )
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
