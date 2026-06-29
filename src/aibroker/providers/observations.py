"""Read/write self-learned provider facts (provider_observations table)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.providers.context_limits import MIN_LEARNABLE_CEILING

log = logging.getLogger(__name__)


async def learned_ceilings() -> dict[str, int]:
    """provider → learned_max_request_tokens for every provider that has one.
    Cheap single scan; called once per chat request to filter the chain."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT provider, learned_max_request_tokens "
            "FROM provider_observations "
            "WHERE learned_max_request_tokens IS NOT NULL"
        ))).all()
    return {r.provider: int(r.learned_max_request_tokens) for r in rows}


async def record_too_large(provider: str, est_tokens: int) -> None:
    """A request of ~est_tokens was rejected as too large by `provider`.
    Store the MIN observed rejection size as the learned ceiling (the tightest
    size we know fails), bump the sample counter. Upsert, best-effort.

    Refuses to learn a ceiling below MIN_LEARNABLE_CEILING — a "too large"
    that small is a misclassified transient (rate-limit/quota), not a real
    size limit. Without this guard LEAST() converged ceilings to ~210 tokens
    and the broker skipped its best free providers on every real prompt."""
    if est_tokens < MIN_LEARNABLE_CEILING:
        return
    now = datetime.now(UTC).replace(tzinfo=None)
    try:
        async with get_session() as s:
            await s.execute(text(
                "INSERT INTO provider_observations "
                "  (provider, learned_max_request_tokens, learned_at, sample_count) "
                "VALUES (:p, :n, :ts, 1) "
                "ON CONFLICT (provider) DO UPDATE SET "
                "  learned_max_request_tokens = "
                "    LEAST(provider_observations.learned_max_request_tokens, :n), "
                "  learned_at = :ts, "
                "  sample_count = provider_observations.sample_count + 1"
            ), {"p": provider, "n": est_tokens, "ts": now})
    except Exception as e:
        log.warning("record_too_large(%s, %d) failed: %s", provider, est_tokens, e)
