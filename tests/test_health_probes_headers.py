"""providers.health_probes — header extraction for auto-discover flow."""
from __future__ import annotations

from aibroker.providers.health_probes import extract_quota_headers


def test_cerebras_x_ratelimit_day_variants():
    """Cerebras returns x-ratelimit-limit-*-day on free tier."""
    headers = {
        "x-ratelimit-limit-requests-day": "14400",
        "x-ratelimit-limit-tokens-day": "1000000",
        "content-type": "application/json",
    }
    req, tok = extract_quota_headers("cerebras", headers)
    assert req == 14_400
    assert tok == 1_000_000


def test_openai_compat_minute_headers_used_as_fallback():
    """When -day variants are absent, fall back to the plain -requests/-tokens
    header (OpenAI-style minute-bucket — operator still gets *something*)."""
    headers = {
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-limit-tokens": "30000",
    }
    req, tok = extract_quota_headers("groq", headers)
    assert req == 30
    assert tok == 30_000


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
    req, _ = extract_quota_headers("cerebras", headers)
    assert req == 14_400


def test_unknown_provider_returns_none_none():
    assert extract_quota_headers("brand-new", {"x-anything": "1"}) == (None, None)
