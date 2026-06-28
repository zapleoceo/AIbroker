"""providers.observations — learned provider ceilings (upsert + read)."""
from __future__ import annotations

import os

import pytest

from aibroker.providers.observations import learned_ceilings, record_too_large

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(
    ON_SQLITE, reason="ON CONFLICT upsert + LEAST need Postgres"
)


async def test_record_then_read():
    await record_too_large("groq", 9000)
    m = await learned_ceilings()
    assert m["groq"] == 9000


async def test_record_keeps_minimum():
    """Second rejection at a smaller size tightens the learned ceiling;
    a larger one does not loosen it."""
    await record_too_large("groq", 9000)
    await record_too_large("groq", 6000)   # tighter → wins
    await record_too_large("groq", 8000)   # looser → ignored
    m = await learned_ceilings()
    assert m["groq"] == 6000


async def test_record_ignores_nonpositive():
    await record_too_large("groq", 0)
    await record_too_large("groq", -5)
    m = await learned_ceilings()
    assert "groq" not in m   # nothing stored


async def test_read_empty_when_none_learned():
    assert await learned_ceilings() == {}
