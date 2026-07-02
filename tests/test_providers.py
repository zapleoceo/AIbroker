"""LiteLLM adapter — model resolution & response parsing."""
from __future__ import annotations

from aibroker.providers.litellm_adapter import estimate_llm_cost, model_for


def test_model_for_known_combos():
    assert model_for("cerebras", "chat:fast").startswith("cerebras/")
    assert model_for("voyage", "embedding") == "voyage/voyage-3"
    assert model_for("gemini", "vision").startswith("gemini/")


def test_model_for_unknown_returns_none():
    assert model_for("zzz", "chat:fast") is None
    assert model_for("cerebras", "made-up-capability") is None


def test_every_chain_pair_resolves_to_a_model():
    """Honest chains: every (provider, capability) in a chain has a DEFAULT_MODEL.

    Without this, a provider listed in a chain returns None from model_for and
    is silently skipped — the chain lies about its real fallback breadth.
    """
    from aibroker.routing.chains import CAPABILITY_CHAINS
    missing = [
        (provider, cap)
        for cap, chain in CAPABILITY_CHAINS.items()
        for provider in chain
        if not model_for(provider, cap)
    ]
    assert not missing, f"providers with no model for their capability: {missing}"


def test_estimate_llm_cost_prices_known_model():
    """cost_per_token maps real pricing — a known model must cost > 0.
    Guards the completion_cost→cost_per_token signature regression that
    silently zeroed every cost and blinded the cost guard for days."""
    cost = estimate_llm_cost("openai/gpt-5", 1_000_000, 1_000_000)
    assert isinstance(cost, float)
    assert cost > 0


def test_estimate_llm_cost_scales_with_tokens():
    small = estimate_llm_cost("deepseek/deepseek-chat", 1_000, 1_000)
    big = estimate_llm_cost("deepseek/deepseek-chat", 1_000_000, 1_000_000)
    assert 0 < small < big


def test_estimate_llm_cost_zero_for_unknown_model():
    # LiteLLM doesn't know about this fictional model → safely returns 0
    cost = estimate_llm_cost("nonexistent/totally-fake", 100, 50)
    assert cost == 0.0


# ─── prompt-cache-aware pricing ──────────────────────────────────────────────


def test_estimate_llm_cost_cache_read_is_cheaper():
    """A cache read (anthropic ~0.1x input rate) must cost less than the same
    prompt priced with no cache info — the old code priced every input token
    at the flat rate, over-charging cache hits."""
    model = "anthropic/claude-sonnet-5"
    no_cache = estimate_llm_cost(model, 10_000, 500)
    with_cache = estimate_llm_cost(model, 10_000, 500, cache_read_tokens=9_000)
    assert 0 < with_cache < no_cache


def test_estimate_llm_cost_cache_write_costs_more_than_flat():
    """Anthropic bills a cache WRITE at a premium over the flat input rate —
    the first call that populates the cache costs slightly more, subsequent
    reads recoup it. Must not be silently ignored/treated as a discount."""
    model = "anthropic/claude-sonnet-5"
    no_cache = estimate_llm_cost(model, 10_000, 500)
    with_write = estimate_llm_cost(model, 10_000, 500, cache_write_tokens=9_000)
    assert with_write > no_cache


def test_estimate_llm_cost_zero_cache_tokens_matches_no_cache_kwargs():
    """Default cache_read_tokens=0/cache_write_tokens=0 must be a true no-op —
    every non-anthropic call site (which never populates cache) is unaffected."""
    model = "deepseek/deepseek-chat"
    assert estimate_llm_cost(model, 1000, 500) == \
        estimate_llm_cost(model, 1000, 500, cache_read_tokens=0, cache_write_tokens=0)
