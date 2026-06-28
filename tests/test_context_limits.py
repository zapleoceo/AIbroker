"""providers.context_limits — size estimate, learned-vs-seed ceiling, too-large detect."""
from __future__ import annotations

from aibroker.providers.context_limits import (
    SEED_MAX_REQUEST_TOKENS,
    effective_ceiling,
    estimate_prompt_tokens,
    fits_context,
    is_too_large_error,
)


def test_estimate_roughly_4_chars_per_token():
    assert estimate_prompt_tokens([{"role": "user", "content": "x" * 4000}]) == 1000


def test_estimate_sums_all_messages():
    msgs = [
        {"role": "system", "content": "a" * 400},
        {"role": "user", "content": "b" * 400},
    ]
    assert estimate_prompt_tokens(msgs) == 200


def test_estimate_handles_missing_or_none_content():
    assert estimate_prompt_tokens([{"role": "user"}, {"content": None}]) == 0


# ─── effective ceiling = min(learned, seed) ──────────────────────────────────


def test_seed_used_when_nothing_learned():
    assert effective_ceiling("groq", None) == 8_000


def test_learned_overrides_when_tighter():
    # provider taught us it 413s at 6k → tighter than 8k seed
    assert effective_ceiling("groq", 6_000) == 6_000


def test_seed_kept_when_learned_is_looser():
    # learned 9k but seed 8k → keep the tighter 8k
    assert effective_ceiling("groq", 9_000) == 8_000


def test_learned_applies_to_provider_with_no_seed():
    # cerebras has no seed; if it ever teaches us a ceiling, use it
    assert effective_ceiling("cerebras", None) is None
    assert effective_ceiling("cerebras", 50_000) == 50_000


# ─── fits_context ────────────────────────────────────────────────────────────


def test_groq_rejects_oversize_prompt():
    assert fits_context("groq", 24_000) is False        # seed 8k×0.9
    assert fits_context("groq", 7_200) is True
    assert fits_context("groq", 7_300) is False


def test_fits_uses_learned_ceiling():
    # learned 4k → 0.9 margin = 3600; 5k prompt no longer fits even though
    # the seed (8k) would have allowed it
    assert fits_context("groq", 5_000, learned=4_000) is False
    assert fits_context("groq", 3_000, learned=4_000) is True


def test_uncapped_providers_always_fit():
    for p in ("cerebras", "gemini", "mistral", "cohere", "anthropic"):
        assert fits_context(p, 1_000_000) is True


def test_unknown_provider_fits_by_default():
    assert fits_context("brand-new-llm", 99_999) is True


# ─── too-large error detection ───────────────────────────────────────────────


def test_too_large_markers_detected():
    for msg in (
        "Error: context length exceeded",
        "maximum context length is 8192 tokens",
        "Request too large for model",
        "413 Payload Too Large",
        "please reduce the length of the messages",
    ):
        assert is_too_large_error(RuntimeError(msg)) is True


def test_plain_rate_limit_is_not_too_large():
    assert is_too_large_error(RuntimeError("429 Too Many Requests")) is False
    assert is_too_large_error(RuntimeError("rate_limit exceeded")) is False


def test_seed_table_groq_only():
    """Regression: groq is the only provider we seed; rest learn or stay open."""
    assert SEED_MAX_REQUEST_TOKENS["groq"] == 8_000
    assert SEED_MAX_REQUEST_TOKENS["cerebras"] is None
