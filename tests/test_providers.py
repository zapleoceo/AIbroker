"""LiteLLM adapter — model resolution & response parsing."""
from __future__ import annotations

from aibroker.providers.litellm_adapter import DEFAULT_MODEL, estimate_llm_cost, model_for


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


def test_estimate_llm_cost_returns_float():
    cost = estimate_llm_cost("openai/gpt-5", 100, 50)
    assert isinstance(cost, float)
    assert cost >= 0


def test_estimate_llm_cost_zero_for_unknown_model():
    # LiteLLM doesn't know about this fictional model → safely returns 0
    cost = estimate_llm_cost("nonexistent/totally-fake", 100, 50)
    assert cost == 0.0
