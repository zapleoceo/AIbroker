"""Per-provider free-tier daily request quotas.

Sourced from each provider's public docs as of 2026-06-28. We track ~order of
magnitude — exact numbers drift, but the dashboard progress bar is for
operator situational awareness, not billing.

Values are 'requests per day per key on the free tier'. None ⇒ paid /
unlimited (no useful percent to draw).
"""
from __future__ import annotations

# Conservative: when unsure, lean low so the bar warns earlier.
PROVIDER_DAILY_QUOTA: dict[str, int | None] = {
    "cerebras":   14_400,   # ~10 RPM × 1440 min = 14.4k; free tier generous
    "groq":       14_400,   # similar RPM-bounded; openai/gpt-oss-120b free
    "gemini":     1_500,    # Gemini 2.5 Flash free tier: 1500 req/day
    "mistral":    86_400,   # 1 RPS sustained = 86k/day on free
    "cohere":     1_000,    # Production tier: 1000 calls/month — pessimistic daily
    "openrouter": 200,      # ":free" models throttled; conservative
    "voyage":     1_000_000,  # 200M tokens/mo — calls effectively unbounded
    # paid / no meaningful daily quota:
    "deepseek":   None,
    "anthropic":  None,
    "openai":     None,
}


def quota_for(provider: str) -> int | None:
    """Daily request quota for a free-tier key. None for paid/unknown."""
    return PROVIDER_DAILY_QUOTA.get(provider)


def percent_used(used: int, provider: str) -> int | None:
    """How much of today's free quota this key has burned. None for paid."""
    q = quota_for(provider)
    if q is None or q <= 0:
        return None
    return min(100, int(used / q * 100))


def severity_class(pct: int | None) -> str:
    """CSS class for the bar fill: blue < 70 → yellow < 90 → red ≥ 90."""
    if pct is None:
        return ""
    if pct >= 90:
        return "bad"
    if pct >= 70:
        return "warn"
    return ""
