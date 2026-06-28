"""providers.context_limits — prompt-size estimate + per-provider fit check."""
from __future__ import annotations

from aibroker.providers.context_limits import (
    PROVIDER_MAX_REQUEST_TOKENS,
    estimate_prompt_tokens,
    fits_context,
)


def test_estimate_roughly_4_chars_per_token():
    msgs = [{"role": "user", "content": "x" * 4000}]
    assert estimate_prompt_tokens(msgs) == 1000


def test_estimate_sums_all_messages():
    msgs = [
        {"role": "system", "content": "a" * 400},
        {"role": "user", "content": "b" * 400},
    ]
    assert estimate_prompt_tokens(msgs) == 200  # 800 chars / 4


def test_estimate_handles_missing_content():
    msgs = [{"role": "user"}, {"role": "user", "content": None}]
    assert estimate_prompt_tokens(msgs) == 0


def test_groq_rejects_oversize_prompt():
    """groq ceiling 8000 × 0.9 margin = 7200. 24k-token prompt → skip."""
    assert fits_context("groq", 24_000) is False
    assert fits_context("groq", 7_000) is True       # under margin
    assert fits_context("groq", 7_200) is True        # exactly at margin
    assert fits_context("groq", 7_300) is False       # over margin


def test_uncapped_providers_always_fit():
    for p in ("cerebras", "gemini", "mistral", "cohere", "anthropic"):
        assert fits_context(p, 30_000) is True
        assert fits_context(p, 1_000_000) is True


def test_unknown_provider_fits_by_default():
    assert fits_context("brand-new-llm", 99_999) is True


def test_groq_has_the_ceiling_others_none():
    """Regression: groq is the only provider we're confident caps single calls."""
    assert PROVIDER_MAX_REQUEST_TOKENS["groq"] == 8_000
    assert PROVIDER_MAX_REQUEST_TOKENS["cerebras"] is None
    assert PROVIDER_MAX_REQUEST_TOKENS["gemini"] is None
