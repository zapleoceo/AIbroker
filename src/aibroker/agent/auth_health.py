"""Catalog health for the single Codex subscription login."""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow

_AUTH_401 = "workspace routing discovery unauthorized (401)"
_CACHE_SECONDS = 30
_cached_until = 0.0
_cached_value = True
_cache_lock = asyncio.Lock()
log = logging.getLogger(__name__)


async def codex_auth_available() -> bool:
    """Mark a confirmed upstream 401 unavailable until a later successful run."""
    global _cached_until, _cached_value
    now = time.monotonic()
    if now < _cached_until:
        return _cached_value
    async with _cache_lock:
        if time.monotonic() < _cached_until:
            return _cached_value
        try:
            async with get_session() as session:
                rows = (await session.execute(
                    select(AgentRunEventRow.type,
                           AgentRunEventRow.payload["error_class"].as_string(),
                           AgentRunEventRow.payload["message"].as_string())
                    .select_from(AgentRunRow)
                    .join(AgentRunEventRow,
                          (AgentRunRow.run_id == AgentRunEventRow.run_id)
                          & (AgentRunRow.last_seq == AgentRunEventRow.seq))
                    .where(AgentRunRow.capability == "agent:codex",
                           AgentRunRow.account_id == "owner",
                           AgentRunRow.outcome.in_(("completed", "failed")),
                           AgentRunEventRow.type.in_(("run.completed", "run.failed")))
                    .order_by(AgentRunRow.last_event_at.desc(), AgentRunRow.run_id.desc())
                    .limit(128)
                )).all()
        except (SQLAlchemyError, OSError):
            log.warning("Codex auth health lookup failed; marking catalog unavailable")
            _cached_value = False
            _cached_until = time.monotonic() + _CACHE_SECONDS
            return False
        for event_type, error_class, message in rows:
            if event_type == "run.completed":
                _cached_value = True
                break
            if error_class == "cli_error" and _AUTH_401 in (message or "").casefold():
                _cached_value = False
                break
        else:
            _cached_value = True
        _cached_until = time.monotonic() + _CACHE_SECONDS
        return _cached_value
