"""Per-provider free-tier daily quotas.

Some providers meter free tier by REQUESTS/day, others by TOKENS/day, some
by both — bar shows whichever you'll hit first. Sourced from each
provider's docs as of 2026-06-28. Verify in your provider console — these
drift; the bar is for situational awareness, not billing accuracy.

Schema: {provider: {"req_per_day": int|None, "tok_per_day": int|None, "doc": url}}
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Quota:
    req_per_day: int | None = None
    tok_per_day: int | None = None
    doc: str = ""


# 2026-06-28 confirmed against user's Cerebras "90% free tokens" notification:
# Cerebras free tier is 1M TOKENS/day (not 14k requests). User was burning
# 1.36M tokens on one key with only 525 requests — explains the alert.
PROVIDER_QUOTAS: dict[str, Quota] = {
    "cerebras": Quota(
        req_per_day=14_400,
        tok_per_day=1_000_000,
        doc="https://inference-docs.cerebras.ai/support/rate-limits",
    ),
    "groq": Quota(
        req_per_day=14_400,
        tok_per_day=500_000,            # gpt-oss-120b free tier ≈500k/day
        doc="https://console.groq.com/docs/rate-limits",
    ),
    "gemini": Quota(
        req_per_day=1_500,              # gemini-2.5-flash free
        tok_per_day=None,               # not daily-token-metered (TPM only)
        doc="https://ai.google.dev/gemini-api/docs/rate-limits",
    ),
    "mistral": Quota(
        req_per_day=86_400,             # 1 RPS sustained
        tok_per_day=500_000,            # roughly — free tier
        doc="https://docs.mistral.ai/deployment/laplateforme/tier/",
    ),
    "cohere": Quota(
        req_per_day=1_000,              # trial: 1k calls/month — pessimistic daily
        tok_per_day=None,
        doc="https://docs.cohere.com/v2/docs/rate-limits",
    ),
    "openrouter": Quota(
        req_per_day=200,                # ":free" models conservative
        tok_per_day=None,
        doc="https://openrouter.ai/docs/api-reference/limits",
    ),
    "voyage": Quota(
        req_per_day=None,               # rate-based, not daily-capped
        tok_per_day=200_000_000,        # 200M tokens/mo ÷ 30 ≈ 6.6M/day; lenient
        doc="https://docs.voyageai.com/docs/pricing",
    ),
    # paid / no meaningful free quota:
    "deepseek":  Quota(doc="https://api-docs.deepseek.com/quick_start/pricing"),
    "anthropic": Quota(doc="https://docs.claude.com/en/docs/about-claude/usage-limits"),
    "openai":    Quota(doc="https://platform.openai.com/docs/guides/rate-limits"),
}


def quota_for(provider: str) -> Quota:
    """Returns the Quota for this provider; empty Quota for unknown."""
    return PROVIDER_QUOTAS.get(provider, Quota())


def percent_used(
    requests_today: int,
    tokens_today: int,
    provider: str,
) -> int | None:
    """How burned today — whichever axis hits first wins.

    Returns the higher of req-pct and tok-pct, capped at 100. None when the
    provider has neither metering (paid).
    """
    q = quota_for(provider)
    pcts: list[int] = []
    if q.req_per_day:
        pcts.append(min(100, int(requests_today / q.req_per_day * 100)))
    if q.tok_per_day:
        pcts.append(min(100, int(tokens_today / q.tok_per_day * 100)))
    if not pcts:
        return None
    return max(pcts)


def severity_class(pct: int | None) -> str:
    """CSS class for the bar fill: blue < 70 → yellow < 90 → red ≥ 90."""
    if pct is None:
        return ""
    if pct >= 90:
        return "bad"
    if pct >= 70:
        return "warn"
    return ""


def bar_label(
    requests_today: int,
    tokens_today: int,
    provider: str,
) -> str:
    """Human-readable 'X/Y' label — picks the axis that's closer to its cap.
    'requests' axis shown as plain integer; 'tokens' shown as `Nk` / `NM`."""
    q = quota_for(provider)
    req_pct = (requests_today / q.req_per_day) if q.req_per_day else 0
    tok_pct = (tokens_today / q.tok_per_day) if q.tok_per_day else 0
    if q.tok_per_day and tok_pct >= req_pct:
        return f"{_humanize(tokens_today)}/{_humanize(q.tok_per_day)} tok"
    if q.req_per_day:
        return f"{requests_today}/{q.req_per_day}"
    return str(requests_today)


def _humanize(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".rstrip("0").rstrip(".") + "M" if False else f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)
