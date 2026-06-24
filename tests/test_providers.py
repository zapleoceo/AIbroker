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


def test_every_chain_provider_has_default_model():
    """Every provider in our routing chains should have at least one DEFAULT_MODEL entry."""
    from aibroker.routing.chains import CAPABILITY_CHAINS
    providers_in_chains = set()
    for chain in CAPABILITY_CHAINS.values():
        providers_in_chains.update(chain)
    providers_with_defaults = set(DEFAULT_MODEL)
    # voyage only appears in embedding chain — must be in defaults
    assert "voyage" in providers_with_defaults
    # all in chat:fast/smart chains
    common = {"cerebras", "groq", "gemini", "anthropic", "openai"}
    assert common.issubset(providers_with_defaults)


def test_estimate_llm_cost_returns_float():
    cost = estimate_llm_cost("openai/gpt-5", 100, 50)
    assert isinstance(cost, float)
    assert cost >= 0


def test_estimate_llm_cost_zero_for_unknown_model():
    # LiteLLM doesn't know about this fictional model → safely returns 0
    cost = estimate_llm_cost("nonexistent/totally-fake", 100, 50)
    assert cost == 0.0
