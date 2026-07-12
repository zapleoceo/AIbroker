"""routing.cooldown — adaptive cooldown table + exponential backoff math."""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")

from aibroker.routing.cooldown import (
    COOLDOWN_BASE_S,
    DEFAULT_COOLDOWN_S,
    MAX_COOLDOWN_S,
    _adaptive_jitter,
    _boundary_jitter,
    cooldown_seconds,
    is_daily_quota_error,
    is_hourly_quota_error,
    is_monthly_quota_error,
    next_hour_boundary,
    next_utc_midnight,
    next_utc_month_start,
    parse_retry_after,
)


def test_first_cooldown_is_provider_base():
    for prov, base in COOLDOWN_BASE_S.items():
        assert cooldown_seconds(prov, 0) == base, f"{prov} should start at base"


def test_adaptive_jitter_within_0_to_25_percent():
    """Anti-herd jitter only ever LENGTHENS a wait, by at most 25%."""
    for _ in range(200):
        j = _adaptive_jitter(60)
        assert 60.0 <= j <= 75.0


def test_boundary_jitter_within_90s():
    """Day/hour reset spread stays in [0, 90s] so it never shortens the wait."""
    for _ in range(200):
        d = _boundary_jitter().total_seconds()
        assert 0.0 <= d <= 90.0


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


def test_parse_retry_after_unitless_and_seconds_word():
    """REGRESSION (2026-07-10): the docstring claimed to support the unitless
    OpenAI-style 'retry after 30' but the regex required a trailing 's'. Now a
    bare number at end-of-string parses; a following non-seconds unit does NOT
    (so 'retry after 30 minutes' is not mis-read as 30 seconds)."""
    assert parse_retry_after("retry after 30") == 30.0
    assert parse_retry_after("please retry after 45 seconds") == 45.0
    assert parse_retry_after("retry after 2 minutes") is None


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


async def test_cooldown_until_honours_retry_hint_above_base():
    """A retry-after hint LONGER than the adaptive base wins — the provider knows
    its own window. (A sub-base hint is floored to the base and escalates when
    the key keeps failing; see the escalation test.)"""
    from aibroker.routing.cooldown import cooldown_until
    until = await cooldown_until(1, "gemini",
                                 "RESOURCE_EXHAUSTED Please retry in 600s.")
    delta = (until - datetime.now(UTC)).total_seconds()
    assert 590 < delta < 620   # ~600s honored (> gemini base 60s)


@pytest.mark.skipif(ON_SQLITE, reason="cross-session usage_log seed needs Postgres")
async def test_cooldown_until_short_hint_escalates_when_key_keeps_failing():
    """REGRESSION (2026-07-10): a free key that 429s every few seconds is
    EXHAUSTED, but Gemini still returns a short retryDelay (~24s). Honouring that
    literally re-picked the dead key ~100x/hr — burning attempts, inflating
    errors, starving reserve keys. cooldown_until now floors a short hint at the
    escalating adaptive backoff, so a repeatedly-failing key gets parked."""
    import os as _os
    from datetime import datetime as _dt

    from sqlalchemy import insert

    from aibroker.crypto import encrypt
    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow, UsageLogRow
    from aibroker.routing.cooldown import cooldown_until

    async with get_session() as s:
        key = ApiKeyRow(provider="gemini", label=f"cd-{_os.urandom(4).hex()}",
                        tier="free", scopes=["llm:chat"],
                        token_encrypted=encrypt("x"))
        s.add(key)
        await s.flush()
        kid = key.id
        for _ in range(6):  # 6 recent 429s → adaptive escalates well past a 5s hint
            await s.execute(insert(UsageLogRow).values(
                api_key_id=kid, provider="gemini", status="error",
                http_status=429, created_at=_dt.now(UTC).replace(tzinfo=None)))
    until = await cooldown_until(kid, "gemini", "RESOURCE_EXHAUSTED retry in 5s.")
    delta = (until - datetime.now(UTC)).total_seconds()
    assert delta > 120   # 5s hint ignored — parked on the escalated backoff


async def test_cooldown_until_mistral_unauthorized_goes_to_next_month():
    """mistral's bare 401 'Unauthorized' carries no monthly marker in its text,
    but on our accounts it IS monthly Vibe exhaustion — the provider-scoped
    rule cools it to next month, not the adaptive few-seconds backoff."""
    from aibroker.routing.cooldown import cooldown_until
    until = await cooldown_until(
        1, "mistral", 'MistralException - {"detail":"Unauthorized"}')
    offset = (until - next_utc_month_start()).total_seconds()
    assert 0 <= offset <= 90                                    # + anti-herd jitter
    assert (until - datetime.now(UTC)).total_seconds() > 86400  # far more than a day
    # Scoped to mistral: the same text for another provider is NOT monthly (it
    # would fall through to the adaptive short backoff, not next-month).
    from aibroker.routing.cooldown import _is_provider_monthly
    assert _is_provider_monthly("mistral", "Unauthorized") is True
    assert _is_provider_monthly("openai", "Unauthorized") is False


async def test_cooldown_until_daily_goes_to_midnight():
    """Daily-quota error with no hint → cool until UTC midnight (not 60s)."""
    from aibroker.routing.cooldown import cooldown_until
    until = await cooldown_until(1, "cerebras",
                                 "CerebrasException - Tokens per day limit exceeded")
    # Next UTC midnight + a small anti-herd jitter (0-90s), far more than 60s
    offset = (until - next_utc_midnight()).total_seconds()
    assert 0 <= offset <= 90
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
    # Top of the next hour + a small anti-herd jitter (0-90s)
    offset = (until - next_hour_boundary()).total_seconds()
    assert 0 <= offset <= 90
    # Between 0 and ~60 min out — always more than the 60s first adaptive step
    delta = (until - datetime.now(UTC)).total_seconds()
    assert 0 < delta <= 3600 + 90


# ─── per-month account/plan cap (2026-07-03: cohere trial "1000 calls/month") ─


def test_is_monthly_quota_error_detects_cohere_trial():
    """The exact real message from an exhausted cohere trial key."""
    assert is_monthly_quota_error(
        "You are using a Trial key, which is limited to 1000 API calls / month."
    )
    assert is_monthly_quota_error("monthly limit reached")


def test_is_monthly_quota_error_false_for_minute_hour_day():
    """'rate limits' (space) in cohere's message must NOT collide with the
    daily/hourly/per-minute markers — those are genuinely different axes."""
    assert not is_monthly_quota_error("requests per minute exceeded")
    assert not is_monthly_quota_error("Tokens per day limit exceeded")
    assert not is_monthly_quota_error("Requests per hour limit exceeded")
    assert not is_monthly_quota_error("429 Too Many Requests")


def test_next_utc_month_start_mid_month():
    base = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
    assert next_utc_month_start(base) == datetime(2026, 8, 1, 0, 0, tzinfo=UTC)


def test_next_utc_month_start_rolls_past_year():
    base = datetime(2026, 12, 15, 8, 0, tzinfo=UTC)
    assert next_utc_month_start(base) == datetime(2027, 1, 1, 0, 0, tzinfo=UTC)


async def test_cooldown_until_monthly_goes_to_next_month():
    """REGRESSION: an exhausted cohere trial key used to fall through
    classify_provider_error to generic 'error' (no cooldown at all, since
    'rate limits' with a space doesn't match 'ratelimit'/'rate_limit') — the
    key was retried on every single pick with zero backoff. A per-month cap
    must park until next month, not a few minutes."""
    from aibroker.routing.cooldown import cooldown_until
    until = await cooldown_until(
        1, "cohere",
        'Cohere_chatException - {"message":"You are using a Trial key, '
        'which is limited to 1000 API calls / month. You can continue to '
        "use the Trial key for free or upgrade to a Production key with "
        'higher rate limits at https://dashboard..."}',
    )
    offset = (until - next_utc_month_start()).total_seconds()
    assert 0 <= offset <= 90                                    # + anti-herd jitter
    assert (until - datetime.now(UTC)).total_seconds() > 86400  # far more than a day


def test_is_daily_quota_error_detects_cloudflare_neurons():
    """cloudflare free tier: 'daily free allocation of 10,000 neurons' is a
    DAILY quota (resets 00:00 UTC) — must park until midnight, not churn."""
    from aibroker.routing.cooldown import is_daily_quota_error
    assert is_daily_quota_error(
        'AiError: you have used up your daily free allocation of 10,000 '
        "neurons, please upgrade to Cloudflare's Workers Paid plan"
    )
