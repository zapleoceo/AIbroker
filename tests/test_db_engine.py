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
    from sqlalchemy import text
    # Re-init via the conftest fixture — close + recreate from scratch.
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
