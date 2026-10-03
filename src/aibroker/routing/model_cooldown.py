"""Per-(key, model) cooldowns.

`api_keys.cooldown_until` parks a whole key. That is wrong for providers that
meter quota PER MODEL per key — Google's Gemini free tier allows 20 requests a
day for EACH model on a key, so one exhausted model must not park the key's
other models (2026-10-03 review). A cooldown here parks one (key, model) pair;
the selector (routing/selector.pick_and_reserve, `models=`) skips a key only
when it is cooled for every model the request could use.

Storage: table api_key_model_cooldowns (migration 014). Writes only ever EXTEND
a cooldown (GREATEST semantics) so a short later 429 cannot shorten a long
daily-quota park. The upsert is portable (no GREATEST()) so the SQLite test
gate exercises it.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db.engine import get_session


def _naive_utc(dt: datetime) -> datetime:
    """cooldown_until is a naive UTC TIMESTAMP (asyncpg rejects tz-aware)."""
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


_UPSERT = text(
    "INSERT INTO api_key_model_cooldowns (api_key_id, model, cooldown_until, reason) "
    "VALUES (:id, :model, :u, :reason) "
    "ON CONFLICT (api_key_id, model) DO UPDATE SET "
    "  cooldown_until = CASE WHEN api_key_model_cooldowns.cooldown_until > excluded.cooldown_until "
    "                        THEN api_key_model_cooldowns.cooldown_until "
    "                        ELSE excluded.cooldown_until END, "
    "  reason = excluded.reason"
)


async def mark_model_cooldown(
    api_key_id: int,
    model: str,
    until: datetime,
    reason: str | None = None,
    *,
    session: AsyncSession | None = None,
) -> None:
    """Park (key, model) until `until`; never shortens an existing cooldown."""
    params = {"id": api_key_id, "model": model[:120], "u": _naive_utc(until),
              "reason": (reason or "")[:200] or None}
    if session is not None:
        await session.execute(_UPSERT, params)
        return
    async with get_session() as s:
        await s.execute(_UPSERT, params)


async def cooled_models(api_key_id: int) -> set[str]:
    """Models of this key that are cooling down right now."""
    now = datetime.now(UTC).replace(tzinfo=None)
    async with get_session() as s:
        rows = await s.execute(
            text("SELECT model FROM api_key_model_cooldowns "
                 "WHERE api_key_id = :id AND cooldown_until > :now"),
            {"id": api_key_id, "now": now},
        )
        return set(rows.scalars().all())
