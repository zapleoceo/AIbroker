"""Read/write self-learned provider facts (provider_observations table)."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.providers.context_limits import MIN_LEARNABLE_CEILING

log = logging.getLogger(__name__)

# A learned ceiling is evidence that a provider rejected a prompt of that size
# AT THAT TIME. Providers raise their limits, switch models and fix bugs, and
# the ceiling is per PROVIDER (not per model/key), so one rejection used to
# bench a provider for large prompts forever (2026-10-03 review). Observations
# older than this are ignored on read and overwritten (not LEAST-ed) on the
# next rejection. `learned_at` is refreshed by every new rejection, so a limit
# that is still real keeps renewing itself; only an unconfirmed one decays.
CEILING_TTL_DAYS = 14


def _ttl_cutoff() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(days=CEILING_TTL_DAYS)


async def learned_ceilings() -> dict[str, int]:
    """provider → learned_max_request_tokens for every provider that has a
    FRESH one (confirmed within CEILING_TTL_DAYS). Cheap single scan; called
    once per chat request to filter the chain."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT provider, learned_max_request_tokens "
            "FROM provider_observations "
            "WHERE learned_max_request_tokens IS NOT NULL "
            "  AND learned_at >= :cutoff"
        ), {"cutoff": _ttl_cutoff()})).all()
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
                # min(existing, new) while the observation is fresh; a stale one
                # (older than the TTL) is replaced outright. CASE, not LEAST():
                # portable, so the SQLite gate covers it.
                "  learned_max_request_tokens = CASE "
                "    WHEN provider_observations.learned_max_request_tokens IS NULL "
                "      OR provider_observations.learned_at IS NULL "
                "      OR provider_observations.learned_at < :cutoff "
                "      OR :n < provider_observations.learned_max_request_tokens "
                "    THEN :n ELSE provider_observations.learned_max_request_tokens END, "
                "  learned_at = :ts, "
                "  sample_count = provider_observations.sample_count + 1"
            ), {"p": provider, "n": est_tokens, "ts": now, "cutoff": _ttl_cutoff()})
    except Exception as e:
        log.warning("record_too_large(%s, %d) failed: %s", provider, est_tokens, e)
