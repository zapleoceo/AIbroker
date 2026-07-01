"""providers.peak_pricing — DeepSeek peak/valley time-of-day multiplier."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from aibroker.providers.litellm_adapter import estimate_llm_cost
from aibroker.providers.peak_pricing import DEEPSEEK_PEAK_FROM, peak_multiplier


def _dt(hour: int, *, day: int = 20, tz=UTC) -> datetime:
    # July 2026, day 20 is safely after DEEPSEEK_PEAK_FROM (2026-07-15).
    return datetime(2026, 7, day, hour, 30, tzinfo=tz)


@pytest.mark.parametrize("hour", [1, 2, 3, 6, 7, 8, 9])
def test_deepseek_peak_hours_double(hour):
    assert peak_multiplier("deepseek", _dt(hour)) == 2.0


@pytest.mark.parametrize("hour", [0, 4, 5, 10, 11, 12, 18, 23])
def test_deepseek_offpeak_hours_flat(hour):
    """04:00 and 10:00 are the exclusive ends of the peak windows — flat."""
    assert peak_multiplier("deepseek", _dt(hour)) == 1.0


def test_dormant_before_start_date():
    """Before mid-July the surcharge doesn't exist yet — always flat."""
    before = DEEPSEEK_PEAK_FROM - timedelta(days=1)
    at = datetime(before.year, before.month, before.day, 2, 30, tzinfo=UTC)
    assert peak_multiplier("deepseek", at) == 1.0


def test_only_deepseek_affected():
    for provider in ("gemini", "anthropic", "openai", "cerebras", "groq"):
        assert peak_multiplier(provider, _dt(2)) == 1.0


def test_naive_datetime_treated_as_utc():
    naive_peak = datetime(2026, 7, 20, 2, 30)  # noqa: DTZ001 — intentional naive
    assert peak_multiplier("deepseek", naive_peak) == 2.0


def test_aware_non_utc_is_converted():
    # 02:30 UTC+8 == 18:30 UTC (off-peak) — must convert, not read local hour.
    plus8 = timezone(timedelta(hours=8))
    assert peak_multiplier("deepseek", datetime(2026, 7, 20, 2, 30, tzinfo=plus8)) == 1.0
    # 10:30 UTC+8 == 02:30 UTC (peak).
    assert peak_multiplier("deepseek", datetime(2026, 7, 20, 10, 30, tzinfo=plus8)) == 2.0


def test_estimate_llm_cost_doubles_deepseek_at_peak():
    off = estimate_llm_cost("deepseek/deepseek-chat", 1_000_000, 1_000_000, at=_dt(12))
    peak = estimate_llm_cost("deepseek/deepseek-chat", 1_000_000, 1_000_000, at=_dt(2))
    assert off > 0
    assert peak == pytest.approx(off * 2.0)


def test_estimate_llm_cost_gemini_flat_at_peak():
    """Non-deepseek models ignore the peak window."""
    off = estimate_llm_cost("gemini/gemini-2.5-flash", 1_000_000, 1_000_000, at=_dt(12))
    peak = estimate_llm_cost("gemini/gemini-2.5-flash", 1_000_000, 1_000_000, at=_dt(2))
    assert off > 0
    assert peak == pytest.approx(off)
