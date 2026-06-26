"""Adaptive cooldown per provider + exponential backoff for repeat offenders.

Why this exists: the old code used a flat 5 min cooldown for every 429,
regardless of provider. Gemini's RPM window resets every 60 s, so 5 min
wastes 4 minutes of throughput per cool-down. OpenRouter overloads can
last minutes, so 30 s would re-spam them. This table encodes each
provider's actual recovery cadence; backoff doubles the wait if the same
key keeps tripping within a window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from aibroker.db.engine import get_session


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
}
DEFAULT_COOLDOWN_S = 300

# Cap on backoff — past this we're wasting requests, the key is just dead.
MAX_COOLDOWN_S = 30 * 60

# Window in which consecutive cool-downs are considered "the same incident"
# for backoff math. Past this, counter resets.
BACKOFF_WINDOW_S = 60 * 60


def cooldown_seconds(provider: str, recent_cooldowns: int) -> int:
    """How long to park a key, given how many times it's tripped in the window."""
    base = COOLDOWN_BASE_S.get(provider, DEFAULT_COOLDOWN_S)
    # 0 prior cooldowns → base; 1 → base*2; 2 → base*4; cap at MAX_COOLDOWN_S.
    return min(base * (2 ** max(0, recent_cooldowns)), MAX_COOLDOWN_S)


async def adaptive_cooldown(api_key_id: int, provider: str) -> datetime:
    """Park a key for an adaptive duration. Returns the UTC `until` timestamp.

    Counts how many times this key was cool-down-marked in the last
    BACKOFF_WINDOW_S; the more recent cool-downs, the longer the next one.
    """
    async with get_session() as s:
        recent = int((await s.execute(
            text(
                "SELECT COUNT(*) FROM usage_log "
                "WHERE api_key_id = :id AND http_status = 429 "
                "  AND created_at > now() - (:w * INTERVAL '1 second')"
            ),
            {"id": api_key_id, "w": BACKOFF_WINDOW_S},
        )).scalar() or 0)
    secs = cooldown_seconds(provider, recent)
    until = datetime.now(timezone.utc) + timedelta(seconds=secs)
    return until
