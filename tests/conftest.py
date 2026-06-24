"""Test fixtures — in-memory SQLite for fast unit tests."""
from __future__ import annotations

import os

# Test-time defaults for env-driven settings (BEFORE any aibroker import).
os.environ.setdefault("SESSION_SECRET", "test-session-secret-not-for-prod")
os.environ.setdefault("OWNER_TELEGRAM_ID", "169510539")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import aibroker.db.engine as engine_mod  # noqa: E402
from aibroker.db.engine import Base  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def in_memory_db():
    """Each test gets a fresh in-memory SQLite engine."""
    e = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    engine_mod._engine = e
    engine_mod._sessionmaker = async_sessionmaker(e, expire_on_commit=False)
    yield
    await e.dispose()
    engine_mod._engine = None
    engine_mod._sessionmaker = None
