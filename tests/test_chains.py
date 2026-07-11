"""Routing chains invariants — free-first, capability completeness."""
from __future__ import annotations

import pytest

from aibroker.routing.chains import (
    CAPABILITY_CHAINS,
    CAPABILITY_SCOPE,
    JSON_UNRELIABLE_PROVIDERS,
    chain_for,
    deprioritize_for_json,
    is_known_capability,
    scope_for,
)

KNOWN_PAID = {"deepseek", "openai", "anthropic"}
KNOWN_FREE = {"cerebras", "groq", "gemini", "openrouter", "sambanova",
              "nvidia", "mistral", "cohere", "voyage", "zai"}


@pytest.mark.parametrize("capability", list(CAPABILITY_CHAINS.keys()))
def test_chain_first_provider_is_known(capability):
    chain = chain_for(capability)
    assert chain, f"{capability} chain is empty"
    assert chain[0] in KNOWN_FREE | KNOWN_PAID


@pytest.mark.parametrize(
    "capability", ["prefilter", "structured", "chat:fast", "chat:smart", "chat:code"]
)
def test_strict_free_first(capability):
    """Strictly-free-first capabilities — no paid before any free.

    2026-07-05: chat:fast/smart/code joined this list — paid (deepseek/
    anthropic/openai) used to sit ahead of github/sambanova/zai "for
    backfill speed", meaning a paid call fired the moment the first ~5 free
    providers were saturated even though more free providers were still
    untried further down the chain. Explicit choice: slow-but-free beats
    fast-but-paid."""
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


def test_chat_fast_paid_at_the_very_tail():
    """2026-07-05: paid providers sit at the LAST entries, after every free
    provider. Paid tail = [deepseek, anthropic, openai] (anthropic re-added
    2026-07-10 after its balance was topped up)."""
    chain = chain_for("chat:fast")
    assert chain[-3:] == ["deepseek", "anthropic", "openai"]


def test_vision_only_vision_providers():
    chain = chain_for("vision")
    # Cerebras / groq / DS don't do vision
    forbidden = {"cerebras", "groq", "deepseek", "voyage"}
    assert not (set(chain) & forbidden)


def test_vision_excludes_anthropic_keeps_openai_fallback():
    """anthropic removed from vision (2026-07-01): it 400'd on Vera's image
    URLs. gemini stays primary, openai is the paid fallback when gemini is
    RPM-exhausted."""
    chain = chain_for("vision")
    assert "anthropic" not in chain
    assert chain[0] == "gemini"
    assert "openai" in chain


def test_vision_has_free_openrouter_fallback():
    """REGRESSION (2026-07-11): vision was [gemini, openai] only. Under load all
    gemini keys cooled at once and there's no openai vision key, so vision jobs
    starved ('no provider available') and hung. openrouter (free llama-3.2-vision,
    a separate key pool) must sit between them. NB cloudflare llava was tried
    first but returned empty completions (0 tokens) — unusable — and anthropic
    400s on image URLs, so neither is eligible."""
    chain = chain_for("vision")
    assert "openrouter" in chain
    assert chain.index("gemini") < chain.index("openrouter")
    assert "cloudflare" not in chain


def test_structured_excludes_cerebras():
    """cerebras dropped from structured (2026-07-01): HTTP-200 malformed JSON."""
    assert "cerebras" not in chain_for("structured")
    # groq (same base model) stays — no InvalidJSON at volume there.
    assert "groq" in chain_for("structured")


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


def test_chat_edit_json_reliable_only():
    """Coach edit chain: JSON-reliable providers ONLY —
    gemini (free) → deepseek → anthropic (paid). Providers that returned
    malformed/Bahasa-drifted JSON (mistral, cohere) or reasoning JSON
    (cerebras, groq, openrouter) are excluded — a bad edit breaks Coach."""
    chain = chain_for("chat:edit")
    assert chain == ["gemini", "deepseek", "anthropic"]
    assert chain[0] == "gemini"
    assert chain.index("gemini") < chain.index("deepseek")
    flaky_json = {"mistral", "cohere", "cerebras", "groq", "openrouter"}
    assert not (flaky_json & set(chain)), \
        f"{flaky_json & set(chain)} produce unreliable JSON — must not serve chat:edit"


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


def test_every_chained_provider_has_a_model_config():
    """Every provider referenced in a capability chain must have a DEFAULT_MODEL
    entry — a chain pointing at a provider with no configured model is a dead
    route that silently always fails over. (Replaces a vacuous `for dead in ():`
    guard that could never fail; this actually asserts the invariant, so adding
    a provider to a chain without wiring its model trips it.)"""
    from aibroker.providers.litellm_adapter import DEFAULT_MODEL
    for cap, chain in CAPABILITY_CHAINS.items():
        for provider in chain:
            assert provider in DEFAULT_MODEL, \
                f"{provider} is routed in {cap} but has no DEFAULT_MODEL entry"


# ─── deprioritize_for_json — JSON-reliable ordering ──────────────────────────


def test_deprioritize_for_json_pushes_unreliable_to_back():
    """Reliable providers keep their order first; unreliable (gpt-oss/cohere)
    sink to the back, also keeping their relative order."""
    chain = ["cerebras", "groq", "gemini", "mistral", "cohere", "deepseek"]
    out = deprioritize_for_json(chain)
    assert out == ["groq", "gemini", "mistral", "deepseek", "cerebras", "cohere"]


def test_deprioritize_for_json_never_drops_a_provider():
    """Even an all-unreliable chain must keep every provider — a maybe-malformed
    retry still beats a 503."""
    chain = ["cerebras", "cohere", "openrouter"]
    out = deprioritize_for_json(chain)
    assert set(out) == set(chain)
    assert len(out) == len(chain)


def test_deprioritize_for_json_groq_stays_reliable():
    """groq runs gpt-oss but is JSON-reliable at volume (grammar-constrained
    mode) — it must NOT be demoted the way cerebras is."""
    assert "groq" not in JSON_UNRELIABLE_PROVIDERS
    assert "cerebras" in JSON_UNRELIABLE_PROVIDERS
    assert deprioritize_for_json(["cerebras", "groq"]) == ["groq", "cerebras"]


def test_deprioritize_for_json_noop_when_all_reliable():
    chain = ["gemini", "mistral", "deepseek", "anthropic"]
    assert deprioritize_for_json(chain) == chain


def test_deprioritize_for_json_demotes_zai():
    """2026-07-05: zai/glm-4.5-flash doesn't support response_format at all
    (confirmed via litellm.get_supported_openai_params) — drop_params=True
    silently strips it, so the model never gets told to emit JSON. Confirmed
    live (request #871336): 200 OK, unparseable body. Must be demoted behind
    JSON-reliable providers on any JSON-format request."""
    assert "zai" in JSON_UNRELIABLE_PROVIDERS
    assert deprioritize_for_json(["zai", "gemini"]) == ["gemini", "zai"]
