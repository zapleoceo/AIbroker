"""providers.health_probes — header extraction for auto-discover flow."""
from __future__ import annotations

from aibroker.providers.health_probes import (
    _parse_duration_seconds,
    extract_quota_headers,
)


def test_parse_duration_milliseconds():
    assert _parse_duration_seconds("547ms") == 0.547


def test_parse_duration_hours_minutes_seconds():
    assert _parse_duration_seconds("1h33m36s") == 3600 + 33 * 60 + 36


def test_parse_duration_days():
    assert _parse_duration_seconds("2d") == 2 * 86400


def test_parse_duration_plain_seconds():
    assert _parse_duration_seconds("2400s") == 2400


def test_parse_duration_empty_or_garbage_returns_none():
    assert _parse_duration_seconds("") is None
    assert _parse_duration_seconds("not-a-duration") is None


def test_cerebras_token_axis_only():
    """Cerebras exposes both -day headers, but its requests-day value isn't a
    hard cap (a key logged 4,866 req against a 2,400 header), so we ingest only
    the token axis to avoid a false dashboard saturation."""
    headers = {
        "x-ratelimit-limit-requests-day": "2400",
        "x-ratelimit-limit-tokens-day": "1000000",
        "content-type": "application/json",
    }
    req, tok = extract_quota_headers("cerebras", headers)
    assert req is None
    assert tok == 1_000_000


def test_bare_header_without_reset_info_is_rejected():
    """No -day/-1d variant AND no reset-* header to confirm the window —
    can't tell if the bare limit is daily or a short rolling bucket, so it
    must NOT be trusted as a daily cap (safer: no data beats wrong data)."""
    headers = {
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-limit-tokens": "30000",
    }
    req, tok = extract_quota_headers("groq", headers)
    assert req is None
    assert tok is None


def test_groq_bare_headers_with_sub_day_reset_are_rejected():
    """REGRESSION: groq's bare x-ratelimit-limit-tokens/-requests are NOT
    daily — confirmed live: tokens reset in ~547ms, requests in ~1h33m36s.
    A key logged 90k-170k tokens/day against an '8000 tokens/day' reading
    from this header — instantly red on the dashboard while perfectly
    healthy. The reset-* header (provider's own signal) must reject both."""
    headers = {
        "x-ratelimit-limit-requests": "1000",
        "x-ratelimit-reset-requests": "1h33m36s",
        "x-ratelimit-limit-tokens": "8000",
        "x-ratelimit-reset-tokens": "547ms",
    }
    req, tok = extract_quota_headers("groq", headers)
    assert req is None
    assert tok is None


def test_bare_header_with_near_24h_reset_is_trusted():
    """When the bare header's OWN reset window genuinely is close to a day,
    it's a legitimate daily reading and should be used."""
    headers = {
        "x-ratelimit-limit-requests": "14400",
        "x-ratelimit-reset-requests": "23h59m",
        "x-ratelimit-limit-tokens": "500000",
        "x-ratelimit-reset-tokens": "24h0m0s",
    }
    req, tok = extract_quota_headers("groq", headers)
    assert req == 14_400
    assert tok == 500_000


def test_anthropic_uses_its_own_prefix():
    headers = {
        "anthropic-ratelimit-requests-limit": "1000",
        "anthropic-ratelimit-tokens-limit": "100000",
    }
    req, tok = extract_quota_headers("anthropic", headers)
    assert req == 1000
    assert tok == 100_000


def test_no_quota_headers_returns_none_none():
    """gemini/cohere/voyage don't return quota headers — discover yields None."""
    for provider in ("gemini", "cohere", "voyage"):
        assert extract_quota_headers(provider, {"content-type": "x"}) == (None, None)


def test_garbage_headers_dont_raise():
    """Negative / non-numeric / zero values all reject cleanly."""
    headers = {
        "x-ratelimit-limit-requests": "not-a-number",
        "x-ratelimit-limit-tokens": "-50",
    }
    assert extract_quota_headers("cerebras", headers) == (None, None)


def test_header_lookup_is_case_insensitive():
    """HTTP headers are case-insensitive — httpx preserves source casing."""
    headers = {"X-RateLimit-Limit-Requests-Day": "14400"}
    req, _ = extract_quota_headers("groq", headers)
    assert req == 14_400


def test_unknown_provider_returns_none_none():
    assert extract_quota_headers("brand-new", {"x-anything": "1"}) == (None, None)
