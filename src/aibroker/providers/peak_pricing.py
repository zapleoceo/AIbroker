"""Time-of-day cost multipliers.

DeepSeek adopts peak/valley pricing from mid-July 2026: peak-hour calls cost
2x across all billing items. Peak hours (UTC): 01:00–04:00 and 06:00–10:00.

We bake the multiplier into the recorded cost (not just the pre-call estimate)
so the bill and every daily $-cap stay accurate — a peak token really is 2x, so
the same budget must buy half as many. One knob, applied in estimate_llm_cost.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

# DeepSeek announced "mid-July 2026" without an exact day; until it lands pricing
# is flat, so the multiplier stays dormant. Set to the confirmed date when known.
DEEPSEEK_PEAK_FROM = date(2026, 7, 15)
# Peak UTC hour buckets: 01:00–04:00 → {1,2,3}; 06:00–10:00 → {6,7,8,9}.
DEEPSEEK_PEAK_HOURS_UTC = frozenset({1, 2, 3, 6, 7, 8, 9})
DEEPSEEK_PEAK_FACTOR = 2.0


def peak_multiplier(provider: str, at: datetime | None = None) -> float:
    """Cost multiplier for `provider` at time `at` (defaults to now, UTC).

    Naive datetimes are treated as UTC (the broker stores naive-UTC everywhere).
    """
    now = at or datetime.now(UTC)
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    if (
        provider == "deepseek"
        and now.date() >= DEEPSEEK_PEAK_FROM
        and now.hour in DEEPSEEK_PEAK_HOURS_UTC
    ):
        return DEEPSEEK_PEAK_FACTOR
    return 1.0
