"""Test fixtures.

Default: in-memory SQLite (fast, no deps) — used for everything except the
Postgres-only selector tests, which `skipif` themselves off SQLite.

CI integration job sets TEST_DATABASE_URL (db name ending `_test`) to a real Postgres; then this fixture
binds the engine to it and the Postgres-only tests run for real.
"""
from __future__ import annotations

import os
import shutil
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
from sqlalchemy import create_engine, event
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


_SQLITE_TEMPLATE: list = []


def _sqlite_template(tmp_path_factory):
    """Schema built once per run into a template file (WAL mode persists in its header);
    each test copies it instead of re-running create_all."""
    if not _SQLITE_TEMPLATE:
        import sqlite3

        path = tmp_path_factory.mktemp("sqlite-template") / "template.db"
        sync_engine = create_engine(f"sqlite:///{path.as_posix()}")
        Base.metadata.create_all(sync_engine)
        sync_engine.dispose()
        con = sqlite3.connect(path)
        con.execute("PRAGMA journal_mode=WAL")  # readers don't block the writer
        con.close()
        _SQLITE_TEMPLATE.append(path)
    return _SQLITE_TEMPLATE[0]


@pytest_asyncio.fixture(autouse=True)
async def db(tmp_path_factory):
    """Fresh schema per test. Postgres when DATABASE_URL targets it, else SQLite.

    SQLite uses a per-test temporary database FILE with NullPool, so every session gets its own
    connection exactly like Postgres (a shared `:memory:` connection let concurrent sessions
    interleave transactions: a rollback in one undid another's flushed work).

    On Postgres we use NullPool: the sync Starlette TestClient runs requests in
    its own event-loop portal, and a pooled asyncpg connection bound to the
    fixture's loop can't be reused there. NullPool opens a fresh connection in
    whatever loop is current, avoiding 'another operation is in progress'.
    """
    if _IS_PG:
        e = create_async_engine(_DB_URL, poolclass=NullPool)
    else:
        db_file = tmp_path_factory.mktemp("sqlite") / "test.db"
        shutil.copyfile(_sqlite_template(tmp_path_factory), db_file)
        e = create_async_engine(
            f"sqlite+aiosqlite:///{db_file.as_posix()}",
            poolclass=NullPool,
            connect_args={"timeout": 30},
        )

        @event.listens_for(e.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):
            # FK enforcement stays at SQLite's default (off), exactly as with :memory:.
            dbapi_conn.execute("PRAGMA synchronous=OFF")  # throwaway file: skip fsync

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
    if not _IS_PG:
        shutil.rmtree(db_file.parent, ignore_errors=True)
    engine_mod._engine = None
    engine_mod._sessionmaker = None


class _EveryProject:
    def __contains__(self, project_id: object) -> bool:
        return True


class _AnyPolicy:
    """A policy that allows anything (the real one is fail-closed; tests/test_agent_policy.py covers it)."""

    models = accounts = frozenset({"*"})

    def allows_model(self, model) -> bool:
        return True

    def allows_account(self, account_id) -> bool:
        return True


@pytest.fixture(autouse=True)
def _agent_allowlist(request, monkeypatch):
    """AGENT_ALLOWED_PROJECT_IDS is fail-closed (empty = nobody). The agent test modules run with
    every project allowed; tests/test_agent_allowlist.py covers the real parsing and opts out."""
    name = request.module.__name__.rsplit(".", 1)[-1]
    if name.startswith("test_agent_") and name != "test_agent_allowlist":
        monkeypatch.setattr("aibroker.agent.constants.allowed_project_ids", lambda: _EveryProject())
    if name.startswith("test_agent_") and name != "test_agent_policy":
        monkeypatch.setattr("aibroker.agent.policy.policy_for", lambda project_id: _AnyPolicy())


@pytest.fixture
def serialized_db(monkeypatch):
    """Keep concurrent agent runs concurrent, but take turns on the database.

    The in-memory SQLite test DB has ONE shared connection, so two runs' transactions interleave
    on it (lost rollbacks, duplicate event `seq`). Production uses separate connections. Every
    DB access of the executor goes through one lock here; adapters/CLIs still run concurrently."""
    import asyncio
    import contextlib
    import functools

    from aibroker.agent import executor

    lock = asyncio.Lock()
    real_get_session = executor.get_session

    @contextlib.asynccontextmanager
    async def locked_session():
        async with lock, real_get_session() as s:
            yield s

    def locked(fn):
        @functools.wraps(fn)
        async def wrapper(*a, **kw):
            async with lock:
                return await fn(*a, **kw)
        return wrapper

    monkeypatch.setattr(executor, "get_session", locked_session)
    for name in ("append_event", "record_usage", "requeue_run", "park_for_quota"):
        monkeypatch.setattr(executor, name, locked(getattr(executor, name)))
