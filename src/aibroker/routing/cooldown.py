"""Adaptive cooldown per provider + exponential backoff for repeat offenders.

Why this exists: the old code used a flat 5 min cooldown for every 429,
regardless of provider. Gemini's RPM window resets every 60 s, so 5 min
wastes 4 minutes of throughput per cool-down. OpenRouter overloads can
last minutes, so 30 s would re-spam them. This table encodes each
provider's actual recovery cadence; backoff doubles the wait if the same
key keeps tripping within a window.
"""
from __future__ import annotations

import random
import re
from datetime import UTC, datetime, time, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db.engine import get_session

# Quota-DURATION marker tables live in providers/provider_errors.py (one home
# for ALL provider-message signs, 2026-07-16); the is_*_quota_error verdicts
# built on them stay here, next to the cooldown math that consumes them.
from aibroker.providers.provider_errors import (
    _DAILY_QUOTA_MARKERS,
    _HOURLY_QUOTA_MARKERS,
    _MONTHLY_QUOTA_MARKERS,
    _PROVIDER_MONTHLY_SIGNS,
)

# Base cooldown in seconds, picked from each provider's published rate-limit
# reset interval. Conservative for paid (we don't want to spam paid keys).
COOLDOWN_BASE_S: dict[str, int] = {
    "cerebras":   60,    # rolling RPM window
    "groq":       60,    # rolling RPM window
    "gemini":     60,    # RPM resets every 60 s for flash
    "mistral":    10,    # 1 RPS — recovers almost instantly
    "cohere":     60,    # 20 RPM trial / per-minute window
    "openrouter": 300,   # ":free" pool overloads can last minutes
    "deepseek":   30,    # paid, fast quotas
    "anthropic":  120,   # paid, conservative
    "openai":     120,   # paid, conservative
    "voyage":     60,    # rolling RPM window
    "sambanova":  120,   # only 20 req/day — don't hammer a near-exhausted key
    "nvidia":     300,   # one-time credits + invisible quota — most conservative
    "cloudflare": 120,   # invisible neuron budget, renews daily — moderate
    "zai":        60,    # no visible quota — moderate default
    "local":      30,    # self-hosted single-worker whisper: a timeout means
                         # the decode lock was busy, NOT a dead credential — re-
                         # probe fast. (Was DEFAULT 300 → 600 on timeout_bump,
                         # parking the free/private/quality path ~10 min per
                         # slow decode and dumping all voice on the paid tail.)
}
DEFAULT_COOLDOWN_S = 300

# Cap on backoff — past this we're wasting requests, the key is just dead.
MAX_COOLDOWN_S = 30 * 60

# Window in which consecutive cool-downs are considered "the same incident"
# for backoff math. Past this, counter resets.
BACKOFF_WINDOW_S = 60 * 60

# Anti-thundering-herd jitter. Keys that trip together (a whole provider's pool
# 429ing at once) would otherwise recover at the same instant and re-storm the
# provider in lockstep. A random spread desynchronises them.
_ADAPTIVE_JITTER_FRAC = 0.25      # adaptive waits stretched by 0-25%
_BOUNDARY_JITTER_S = 90           # day/hour resets spread 0-90s past the boundary


def cooldown_seconds(provider: str, recent_cooldowns: int, *,
                     timeout_bump: bool = False) -> int:
    """How long to park a key, given how many times it's tripped in the window.

    `timeout_bump` escalates one strike faster: a TimeoutError wasted ~60s of
    wall-clock (vs 0s for a 429), so a hanging key should drop out of rotation
    in ~2 strikes, not ~5 (2026-07-16 storm — hung keys kept getting re-picked
    on a short adaptive wait and re-hung, flooring throughput)."""
    base = COOLDOWN_BASE_S.get(provider, DEFAULT_COOLDOWN_S)
    # 0 prior cooldowns → base; 1 → base*2; 2 → base*4; cap at MAX_COOLDOWN_S.
    steps = recent_cooldowns + (1 if timeout_bump else 0)
    return min(base * (2 ** max(0, steps)), MAX_COOLDOWN_S)


def _adaptive_jitter(secs: int) -> float:
    """Stretch an adaptive wait by a random 0-25% so peers don't recover as one."""
    return secs * random.uniform(1.0, 1.0 + _ADAPTIVE_JITTER_FRAC)


def _boundary_jitter() -> timedelta:
    """A small random offset past a day/hour reset, so a provider's keys don't
    all wake at the exact same tick."""
    return timedelta(seconds=random.uniform(0, _BOUNDARY_JITTER_S))


# Providers tell us exactly how long to wait via a retry hint — honour it
# instead of guessing. Covers Gemini "Please retry in 24.5s", OpenAI-style
# "retry after 30", Google "retryDelay: 24s".
# The number is followed by a seconds unit (s / sec / seconds) OR the end of
# the string — the latter catches the unitless "retry after 30". Deliberately
# NOT a bare number mid-string: "retry after 30 minutes" must NOT parse as 30s,
# so a trailing non-seconds word fails the match (falls through to adaptive).
_RETRY_AFTER_RE = re.compile(
    r"(?:retry(?:[ -]?after| in)|retrydelay)\D{0,4}?(\d+(?:\.\d+)?)"
    r"\s*(?:s(?:ec(?:onds?)?)?\b|$)",
    re.IGNORECASE,
)


def parse_retry_after(msg: str) -> float | None:
    """Seconds the provider asked us to wait, if it said so. None otherwise."""
    m = _RETRY_AFTER_RE.search(msg)
    if not m:
        return None
    try:
        secs = float(m.group(1))
    except ValueError:
        return None
    return secs if 0 < secs <= MAX_COOLDOWN_S else None


def is_daily_quota_error(msg: str) -> bool:
    """True if the error is a per-DAY quota exhaustion (not a per-minute limit)."""
    m = msg.lower()
    return any(marker in m for marker in _DAILY_QUOTA_MARKERS)


def is_hourly_quota_error(msg: str) -> bool:
    """True if the error is a per-HOUR request cap (not per-minute or per-day)."""
    m = msg.lower()
    return any(marker in m for marker in _HOURLY_QUOTA_MARKERS)


def next_utc_midnight(now: datetime | None = None) -> datetime:
    """First instant of the next UTC day — when daily quotas reset."""
    now = now or datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).date()
    return datetime.combine(tomorrow, time.min, tzinfo=UTC)


def next_hour_boundary(now: datetime | None = None) -> datetime:
    """Top of the next UTC hour — a safe wait for a per-hour request cap."""
    now = now or datetime.now(UTC)
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def is_monthly_quota_error(msg: str) -> bool:
    """True if the error is a per-MONTH account/plan cap (e.g. a trial key's
    call allowance), not a per-minute/hour/day rate limit."""
    m = msg.lower()
    return any(marker in m for marker in _MONTHLY_QUOTA_MARKERS)


def _is_provider_monthly(provider: str, msg: str) -> bool:
    m = msg.lower()
    return any(s in m for s in _PROVIDER_MONTHLY_SIGNS.get(provider, ()))


def next_utc_month_start(now: datetime | None = None) -> datetime:
    """First instant of next UTC calendar month — when a monthly call
    allowance (e.g. a trial-tier plan) resets."""
    now = now or datetime.now(UTC)
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=UTC)
    return datetime(now.year, now.month + 1, 1, tzinfo=UTC)


async def cooldown_until(
    api_key_id: int,
    provider: str,
    error_msg: str,
    *,
    session: AsyncSession | None = None,
    is_timeout: bool = False,
) -> datetime:
    """Resolve the cooldown end for a rate-limited call, most-authoritative first:
      1. provider's own retry-after hint  → wait exactly that
      2. monthly account/plan cap (no hint) → wait until next UTC calendar month
      3. daily-quota exhaustion (no hint)  → wait until UTC midnight
      4. hourly request cap (no hint)      → wait to the top of the next hour
      5. otherwise                         → adaptive per-provider backoff

    `session` (optional) lets the caller run the adaptive COUNT inside its own
    transaction — _penalize merges the whole penalty into ONE session instead
    of one per statement (this path fires on every failed attempt).

    `is_timeout` steepens the adaptive backoff (a hung key wasted ~60s and must
    drop out faster than a 0s-wasted 429 — 2026-07-16 storm).
    """
    retry = parse_retry_after(error_msg)
    if retry is not None:
        # Honour the provider's hint — BUT never park for LESS than the
        # escalating adaptive backoff. A free key that keeps 429-ing every few
        # seconds is EXHAUSTED (e.g. daily quota used up), not momentarily
        # throttled, yet Gemini still returns a short retryDelay (~24s) for it.
        # Trusting that literally re-picked the dead key ~100x/hr — burning
        # attempts, inflating errors, and starving reserve keys (the chain never
        # exhausts the shared pool, so is_reserve keys are never reached). Taking
        # the max with the adaptive escalation parks a repeatedly-failing key for
        # up to MAX_COOLDOWN_S so it drops out of rotation; a one-off blip (low
        # recent count → tiny adaptive) still just waits the provider's hint.
        retry_until = datetime.now(UTC) + timedelta(seconds=retry)
        adaptive = await adaptive_cooldown(api_key_id, provider, session=session,
                                           timeout_bump=is_timeout)
        return max(retry_until, adaptive)
    if is_monthly_quota_error(error_msg) or _is_provider_monthly(provider, error_msg):
        return next_utc_month_start() + _boundary_jitter()
    if is_daily_quota_error(error_msg):
        return next_utc_midnight() + _boundary_jitter()
    if is_hourly_quota_error(error_msg):
        return next_hour_boundary() + _boundary_jitter()
    return await adaptive_cooldown(api_key_id, provider, session=session,
                                   timeout_bump=is_timeout)


async def _recent_429_count(s: AsyncSession, api_key_id: int, since: datetime) -> int:
    return int((await s.execute(
        text(
            "SELECT COUNT(*) FROM usage_log "
            "WHERE api_key_id = :id AND http_status = 429 "
            "  AND created_at > :since"
        ),
        {"id": api_key_id, "since": since},
    )).scalar() or 0)


async def adaptive_cooldown(
    api_key_id: int,
    provider: str,
    *,
    session: AsyncSession | None = None,
    timeout_bump: bool = False,
) -> datetime:
    """Park a key for an adaptive duration. Returns the UTC `until` timestamp.

    Counts how many times this key was cool-down-marked in the last
    BACKOFF_WINDOW_S; the more recent cool-downs, the longer the next one.
    `timeout_bump` escalates one strike faster for a hung key.
    """
    # Threshold computed in Python (not Postgres `now() - INTERVAL`) so the query
    # is portable to the SQLite test DB — cooldown_until now reaches this on the
    # retry-after path too, which the SQLite deploy gate exercises.
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=BACKOFF_WINDOW_S)
    if session is not None:
        recent = await _recent_429_count(session, api_key_id, since)
    else:
        async with get_session() as s:
            recent = await _recent_429_count(s, api_key_id, since)
    secs = _adaptive_jitter(cooldown_seconds(provider, recent, timeout_bump=timeout_bump))
    return datetime.now(UTC) + timedelta(seconds=secs)
