"""Async SQLAlchemy engine singleton + scoped session for FastAPI."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from aibroker.config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Single declarative base for the broker schema."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


async def init_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        return
    s = get_settings()
    # asyncpg prepares statements implicitly; prod DATABASE_URL points at
    # PgBouncer in TRANSACTION pooling mode, which historically broke prepared
    # statements ("prepared statement ... does not exist"). It works here ONLY
    # because docker-compose pins pgbouncer with MAX_PREPARED_STATEMENTS=500
    # (supported since pgbouncer 1.21; verified live on 1.25 — it tracks named
    # prepared statements across pooled connections). Do NOT "fix" asyncpg
    # prepared-statement errors by adding
    # connect_args={"statement_cache_size": 0} — that trades a config problem
    # for a per-query re-parse on the hottest path; restore the compose env
    # instead. Guarded by tests/test_db_engine.py's compose assertion.
    _engine = create_async_engine(
        s.DATABASE_URL,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    _sessionmaker = async_sessionmaker(
        _engine, expire_on_commit=False, class_=AsyncSession
    )
    log.info("DB engine initialised")


async def close_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _sessionmaker = None


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Use as `async with get_session() as s:` — auto-commit on success."""
    if _sessionmaker is None:
        raise RuntimeError("Engine not initialised. Call init_engine() first.")
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
