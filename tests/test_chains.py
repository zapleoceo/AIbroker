"""Routing chains invariants — free-first, capability completeness."""
from __future__ import annotations

import pytest

from aibroker.routing.chains import CAPABILITY_CHAINS, chain_for


KNOWN_PAID = {"deepseek", "openai", "anthropic"}
KNOWN_FREE = {"cerebras", "groq", "gemini", "openrouter", "sambanova",
              "nvidia", "mistral", "voyage"}


@pytest.mark.parametrize("capability", list(CAPABILITY_CHAINS.keys()))
def test_chain_first_provider_is_known(capability):
    chain = chain_for(capability)
    assert chain, f"{capability} chain is empty"
    assert chain[0] in KNOWN_FREE | KNOWN_PAID


@pytest.mark.parametrize("capability", ["prefilter", "structured"])
def test_strict_free_first(capability):
    """Strictly-free-first capabilities — no paid before any free."""
    chain = chain_for(capability)
    paid_idx = [i for i, p in enumerate(chain) if p in KNOWN_PAID]
    free_idx = [i for i, p in enumerate(chain) if p in KNOWN_FREE]
    if paid_idx and free_idx:
        assert max(free_idx) < min(paid_idx), (
            f"{capability}: free providers must precede all paid"
        )


@pytest.mark.parametrize("capability", ["chat:fast", "chat:smart", "chat:code"])
def test_chat_first_3_are_free(capability):
    """Documented invariant: chat:* chains always START with at least 3 free providers."""
    chain = chain_for(capability)
    for provider in chain[:3]:
        assert provider in KNOWN_FREE, (
            f"{capability}: first 3 must be free, got {provider}"
        )


def test_chat_fast_documented_exception():
    """chat:fast intentionally puts deepseek after the 3 top free for backfill speed."""
    chain = chain_for("chat:fast")
    deepseek_idx = chain.index("deepseek")
    for must_precede in ("cerebras", "groq", "gemini"):
        assert chain.index(must_precede) < deepseek_idx, (
            f"{must_precede} must precede deepseek in chat:fast"
        )


def test_vision_only_vision_providers():
    chain = chain_for("vision")
    # Cerebras / groq / DS don't do vision
    forbidden = {"cerebras", "groq", "deepseek", "voyage"}
    assert not (set(chain) & forbidden)


def test_embedding_only_voyage():
    chain = chain_for("embedding")
    assert chain == ["voyage"]


def test_unknown_capability_raises():
    with pytest.raises(ValueError, match="unknown capability"):
        chain_for("nope-not-real")  # type: ignore[arg-type]


def test_chain_returns_copy():
    """chain_for must return a list copy — mutating shouldn't leak into the global."""
    c1 = chain_for("chat:fast")
    c1.append("hacked")
    c2 = chain_for("chat:fast")
    assert "hacked" not in c2
