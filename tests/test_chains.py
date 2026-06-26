"""Routing chains invariants — free-first, capability completeness."""
from __future__ import annotations

import pytest

from aibroker.routing.chains import (
    CAPABILITY_CHAINS,
    CAPABILITY_SCOPE,
    chain_for,
    is_known_capability,
    scope_for,
)

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


def test_embedding_chain_voyage_first():
    """voyage is the primary embedder; cohere is the fallback."""
    chain = chain_for("embedding")
    assert chain[0] == "voyage"
    assert "cohere" in chain


def test_unknown_capability_raises():
    with pytest.raises(ValueError, match="unknown capability"):
        chain_for("nope-not-real")  # type: ignore[arg-type]


def test_chain_returns_copy():
    """chain_for must return a list copy — mutating shouldn't leak into the global."""
    c1 = chain_for("chat:fast")
    c1.append("hacked")
    c2 = chain_for("chat:fast")
    assert "hacked" not in c2


# ─── chat:edit — Coach lane ──────────────────────────────────────────────────


def test_chat_edit_gemini_first_deepseek_fallback():
    """Coach edit chain mirrors Stepan's local: gemini first (free, best JSON),
    deepseek the always-available paid fallback when gemini quota is dry."""
    chain = chain_for("chat:edit")
    assert chain[0] == "gemini"
    assert "deepseek" in chain
    assert chain.index("gemini") < chain.index("deepseek")


def test_chat_edit_uses_edit_scope():
    assert scope_for("chat:edit") == "llm:edit"


# ─── CAPABILITY_SCOPE — single source of truth ───────────────────────────────


def test_every_capability_has_a_scope():
    """No chain may exist without a declared scope (drift guard)."""
    assert set(CAPABILITY_SCOPE) == set(CAPABILITY_CHAINS)


@pytest.mark.parametrize("capability", list(CAPABILITY_CHAINS.keys()))
def test_scope_for_known(capability):
    assert scope_for(capability).startswith("llm:")


def test_scope_for_unknown_raises():
    with pytest.raises(ValueError, match="unknown capability"):
        scope_for("nope")  # type: ignore[arg-type]


def test_is_known_capability():
    assert is_known_capability("chat:edit")
    assert not is_known_capability("nope")


def test_dead_providers_not_in_any_chain():
    """Providers without DEFAULT_MODEL entries must not be in any chain.
    Mistral + cohere were added 2026-06-26 — they have DEFAULT_MODEL coverage
    so are no longer dead."""
    for cap, chain in CAPABILITY_CHAINS.items():
        for dead in ("sambanova", "nvidia"):
            assert dead not in chain, f"{dead} still routed in {cap}"
