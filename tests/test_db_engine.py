"""db/engine — init/close lifecycle + session commit/rollback."""
from __future__ import annotations

import pytest

import aibroker.db.engine as engine_mod
from aibroker.db.engine import close_engine, get_session, init_engine


async def test_init_engine_idempotent(monkeypatch):
    """Calling init_engine twice is a no-op (singleton)."""
    # in_memory_db autouse fixture already set _engine — capture it
    before = engine_mod._engine
    await init_engine()
    assert engine_mod._engine is before


async def test_close_engine_clears_singletons():
    """After close, _engine and _sessionmaker are None."""
    await close_engine()
    assert engine_mod._engine is None
    assert engine_mod._sessionmaker is None


async def test_get_session_without_init_raises():
    """If the singleton is None, get_session() raises RuntimeError."""
    await close_engine()
    with pytest.raises(RuntimeError, match="Engine not initialised"):
        async with get_session():
            pass


async def test_get_session_rollback_on_exception():
    """Exception inside the with-block triggers rollback."""
    # Re-init via the conftest fixture — close + recreate from scratch.
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from aibroker.db.engine import Base
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._sessionmaker = async_sessionmaker(e, expire_on_commit=False)

    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        async with get_session() as s:
            await s.execute(text("SELECT 1"))
            raise Boom("rollback me")
    # If rollback path didn't fire, the next session would carry the half-tx
    async with get_session() as s:
        r = await s.execute(text("SELECT 2"))
        assert r.scalar_one() == 2

    await e.dispose()
    engine_mod._engine = None
    engine_mod._sessionmaker = None


def test_compose_pins_pgbouncer_max_prepared_statements():
    """asyncpg's implicit prepared statements survive PgBouncer's TRANSACTION
    pooling ONLY because compose sets MAX_PREPARED_STATEMENTS (pgbouncer >=
    1.21 tracks named prepared statements across pooled connections; verified
    live on 1.25). If this env var disappears, prod starts throwing
    'prepared statement ... does not exist' — and the WRONG fix (asyncpg
    statement_cache_size=0) costs a re-parse on every hot-path query. See the
    comment in aibroker/db/engine.py init_engine."""
    from pathlib import Path

    compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    assert "MAX_PREPARED_STATEMENTS" in text, (
        "docker-compose.yml no longer sets MAX_PREPARED_STATEMENTS on "
        "pgbouncer — asyncpg + transaction pooling is unsafe without it"
    )


async def test_init_engine_applies_pool_settings(monkeypatch):
    """pool_size / max_overflow come from settings, not literals."""
    from aibroker.config import get_settings

    await close_engine()
    seen: dict = {}

    def fake_create(url, **kw):
        seen.update(kw)
        raise RuntimeError("stop")

    monkeypatch.setattr(engine_mod, "create_async_engine", fake_create)
    monkeypatch.setattr(
        engine_mod, "get_settings",
        lambda: get_settings().model_copy(update={"DB_POOL_SIZE": 2, "DB_MAX_OVERFLOW": 3}),
    )
    with pytest.raises(RuntimeError, match="stop"):
        await init_engine()
    assert (seen["pool_size"], seen["max_overflow"]) == (2, 3)
    assert seen["pool_pre_ping"] is True


def test_pool_defaults_unchanged_and_compose_overrides():
    import re
    from pathlib import Path

    from aibroker.config import get_settings

    s = get_settings()
    assert (s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW) == (10, 20)
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text(encoding="utf-8")
    assert len(re.findall(r'DB_POOL_SIZE: "2"', compose)) == 3          # monitor + 2 claude workers
    assert len(re.findall(r'DB_MAX_OVERFLOW: "3"', compose)) == 3
    # the codex worker runs AGENT_CONCURRENCY_CODEX (6-7) runs in parallel: a larger pool
    assert len(re.findall(r'DB_POOL_SIZE: "4"', compose)) == 1
    assert len(re.findall(r'DB_MAX_OVERFLOW: "6"', compose)) == 1
