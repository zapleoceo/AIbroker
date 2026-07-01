"""routing.cooldown — adaptive cooldown table + exponential backoff math."""
from __future__ import annotations

from datetime import UTC, datetime

from aibroker.routing.cooldown import (
    COOLDOWN_BASE_S,
    DEFAULT_COOLDOWN_S,
    MAX_COOLDOWN_S,
    cooldown_seconds,
    is_daily_quota_error,
    is_hourly_quota_error,
    next_hour_boundary,
    next_utc_midnight,
    parse_retry_after,
)


def test_first_cooldown_is_provider_base():
    for prov, base in COOLDOWN_BASE_S.items():
        assert cooldown_seconds(prov, 0) == base, f"{prov} should start at base"


def test_unknown_provider_uses_default():
    assert cooldown_seconds("brand-new-llm", 0) == DEFAULT_COOLDOWN_S


def test_exponential_backoff_doubles():
    """Each consecutive cooldown within the window doubles the wait."""
    # gemini base = 60s
    assert cooldown_seconds("gemini", 0) == 60
    assert cooldown_seconds("gemini", 1) == 120
    assert cooldown_seconds("gemini", 2) == 240
    assert cooldown_seconds("gemini", 3) == 480
    # mistral base = 10s, doubles too
    assert cooldown_seconds("mistral", 0) == 10
    assert cooldown_seconds("mistral", 1) == 20
    assert cooldown_seconds("mistral", 2) == 40


def test_backoff_caps_at_max():
    """Never wait longer than MAX_COOLDOWN_S no matter how many failures."""
    assert cooldown_seconds("gemini", 20) == MAX_COOLDOWN_S
    assert cooldown_seconds("openrouter", 20) == MAX_COOLDOWN_S
    assert cooldown_seconds("anything", 50) == MAX_COOLDOWN_S


def test_gemini_recovers_in_one_minute_first_try():
    """Regression: Gemini's RPM window is 60s — base must match.

    Old flat 5min wasted 4 minutes per Gemini cooldown.
    """
    assert COOLDOWN_BASE_S["gemini"] == 60


def test_openrouter_stays_conservative():
    """Regression: :free pool overload can last minutes — don't re-spam."""
    assert COOLDOWN_BASE_S["openrouter"] >= 120


def test_paid_providers_get_long_cooldown():
    """Don't burn paid credits by retrying every few seconds on 429."""
    for p in ("anthropic", "openai"):
        assert COOLDOWN_BASE_S[p] >= 60, f"{p} base too short"


# ─── daily-quota vs per-minute distinction (2026-06-29 bug fix) ──────────────


def test_parse_retry_after_gemini():
    """Gemini: 'Please retry in 24.5s' → honour the provider's own hint."""
    msg = "RESOURCE_EXHAUSTED ... Please retry in 24.519043651s."
    assert parse_retry_after(msg) == 24.519043651


def test_parse_retry_after_variants():
    assert parse_retry_after("retry after 30s") == 30.0
    assert parse_retry_after("retryDelay: 12s") == 12.0
    assert parse_retry_after("Retry in 5s please") == 5.0


def test_parse_retry_after_absent():
    assert parse_retry_after("CerebrasException - Tokens per day limit exceeded") is None
    assert parse_retry_after("plain rate limit") is None


def test_parse_retry_after_caps_absurd_values():
    # > MAX_COOLDOWN_S (30 min) is ignored — don't park a key for hours on a
    # weird hint; fall through to other logic.
    assert parse_retry_after("retry in 99999s") is None


def test_is_daily_quota_error_detects_cerebras():
    """Cerebras 'Tokens per day limit exceeded' → daily, not per-minute."""
    assert is_daily_quota_error("CerebrasException - Tokens per day limit exceeded")
    assert is_daily_quota_error("requests per day exceeded")
    assert is_daily_quota_error("daily limit reached")


def test_is_daily_quota_error_false_for_per_minute():
    assert not is_daily_quota_error("rate limit: requests per minute exceeded")
    assert not is_daily_quota_error("429 Too Many Requests")
    assert not is_daily_quota_error("tokens per minute (TPM) limit")


def test_next_utc_midnight_is_future_and_at_zero():
    base = datetime(2026, 6, 29, 14, 30, tzinfo=UTC)
    nm = next_utc_midnight(base)
    assert nm == datetime(2026, 6, 30, 0, 0, tzinfo=UTC)
    assert nm > base


def test_next_utc_midnight_just_before_midnight():
    base = datetime(2026, 6, 29, 23, 59, 59, tzinfo=UTC)
    assert next_utc_midnight(base) == datetime(2026, 6, 30, 0, 0, tzinfo=UTC)


async def test_cooldown_until_prefers_retry_hint():
    """retry-after hint wins over everything — no DB needed (adaptive not hit)."""
    from aibroker.routing.cooldown import cooldown_until
    until = await cooldown_until(1, "gemini",
                                 "RESOURCE_EXHAUSTED Please retry in 30s.")
    delta = (until - datetime.now(UTC)).total_seconds()
    assert 25 < delta < 35   # ~30s


async def test_cooldown_until_daily_goes_to_midnight():
    """Daily-quota error with no hint → cool until UTC midnight (not 60s)."""
    from aibroker.routing.cooldown import cooldown_until
    until = await cooldown_until(1, "cerebras",
                                 "CerebrasException - Tokens per day limit exceeded")
    # Must be the next UTC midnight, far more than a 60s adaptive cooldown
    assert until == next_utc_midnight()
    assert (until - datetime.now(UTC)).total_seconds() > 120


# ─── per-hour request cap (2026-07-01: cerebras "Requests per hour") ──────────


def test_is_hourly_quota_error_detects_cerebras():
    assert is_hourly_quota_error("CerebrasException - Requests per hour limit exceeded")
    assert is_hourly_quota_error("hourly limit reached")


def test_is_hourly_quota_error_false_for_minute_and_day():
    assert not is_hourly_quota_error("requests per minute exceeded")
    assert not is_hourly_quota_error("Tokens per day limit exceeded")
    assert not is_hourly_quota_error("429 Too Many Requests")


def test_next_hour_boundary_is_top_of_next_hour():
    base = datetime(2026, 7, 1, 8, 38, 25, tzinfo=UTC)
    assert next_hour_boundary(base) == datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC)


def test_next_hour_boundary_rolls_past_day():
    base = datetime(2026, 7, 1, 23, 30, tzinfo=UTC)
    assert next_hour_boundary(base) == datetime(2026, 7, 2, 0, 0, 0, tzinfo=UTC)


async def test_cooldown_until_hourly_goes_to_next_hour():
    """Per-hour cap with no retry hint → cool to the top of the next hour, not
    a 60s adaptive step that re-hits the wall."""
    from aibroker.routing.cooldown import cooldown_until
    until = await cooldown_until(1, "cerebras",
                                 "CerebrasException - Requests per hour limit exceeded")
    assert until == next_hour_boundary()
    # Between 0 and 60 min out — always more than the 60s first adaptive step
    delta = (until - datetime.now(UTC)).total_seconds()
    assert 0 < delta <= 3600
