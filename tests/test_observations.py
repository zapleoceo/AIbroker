"""providers.observations — learned provider ceilings (upsert + read)."""
from __future__ import annotations

from aibroker.providers.observations import learned_ceilings, record_too_large


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


async def test_record_ignores_below_floor():
    """REGRESSION (2026-06-29): a 'too large' under MIN_LEARNABLE_CEILING is a
    misclassified transient — never learn it. Previously these tiny values
    (e.g. 210) became the ceiling and broke routing."""
    await record_too_large("groq", 210)
    await record_too_large("cerebras", 3999)
    m = await learned_ceilings()
    assert "groq" not in m
    assert "cerebras" not in m


async def test_record_accepts_at_floor():
    await record_too_large("groq", 4000)   # exactly the floor → learned
    m = await learned_ceilings()
    assert m["groq"] == 4000


async def test_read_empty_when_none_learned():
    assert await learned_ceilings() == {}


async def _age(provider: str, days: int) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from aibroker.db import get_session
    async with get_session() as s:
        await s.execute(
            text("UPDATE provider_observations SET learned_at = :t WHERE provider = :p"),
            {"t": datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days), "p": provider})


async def test_stale_ceiling_is_ignored_on_read():
    """Per-provider ceilings used to live forever; a limit not re-confirmed
    within CEILING_TTL_DAYS no longer benches the provider."""
    from aibroker.providers.observations import CEILING_TTL_DAYS
    await record_too_large("groq", 9000)
    await _age("groq", CEILING_TTL_DAYS + 1)
    assert "groq" not in await learned_ceilings()


async def test_ceiling_within_ttl_is_kept():
    from aibroker.providers.observations import CEILING_TTL_DAYS
    await record_too_large("groq", 9000)
    await _age("groq", CEILING_TTL_DAYS - 1)
    assert (await learned_ceilings())["groq"] == 9000


async def test_new_rejection_replaces_a_stale_ceiling_instead_of_min():
    """A stale 5000 must not clamp a fresh 9000 observation (LEAST would)."""
    from aibroker.providers.observations import CEILING_TTL_DAYS
    await record_too_large("groq", 5000)
    await _age("groq", CEILING_TTL_DAYS + 5)
    await record_too_large("groq", 9000)
    assert (await learned_ceilings())["groq"] == 9000


async def test_fresh_rejections_still_keep_the_minimum():
    await record_too_large("groq", 9000)
    await record_too_large("groq", 6000)
    await record_too_large("groq", 8000)
    assert (await learned_ceilings())["groq"] == 6000
