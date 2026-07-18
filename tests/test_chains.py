"""Routing chains invariants — free-first, capability completeness."""
from __future__ import annotations

import pytest

from aibroker.routing.chains import (
    CAPABILITY_CHAINS,
    CAPABILITY_SCOPE,
    JSON_INCAPABLE_PROVIDERS,
    JSON_UNRELIABLE_PROVIDERS,
    chain_for,
    deprioritize_for_json,
    has_paid_tail,
    is_known_capability,
    scope_for,
)

KNOWN_PAID = {"deepseek", "openai", "anthropic"}
KNOWN_FREE = {"cerebras", "groq", "gemini", "openrouter", "sambanova",
              "nvidia", "mistral", "cohere", "voyage", "zai", "local"}


@pytest.mark.parametrize("capability", list(CAPABILITY_CHAINS.keys()))
def test_chain_first_provider_is_known(capability):
    chain = chain_for(capability)
    assert chain, f"{capability} chain is empty"
    assert chain[0] in KNOWN_FREE | KNOWN_PAID


def test_has_paid_tail_gates_the_final_retry_escalation():
    """The final-retry paid_only escalation is only meaningful where the chain
    reaches a paid provider with a wired model. chat:deep is nvidia-only (free),
    so demanding a paid key there is a guaranteed no-op."""
    assert has_paid_tail("chat:fast") is True     # deepseek/anthropic/openai tail
    assert has_paid_tail("chat:edit") is True      # deepseek + anthropic wired
    assert has_paid_tail("chat:deep") is False      # nvidia-only free lane


@pytest.mark.parametrize(
    "capability", ["prefilter", "structured", "chat:fast", "chat:code"]
)
def test_strict_free_first(capability):
    """Strictly-free-first capabilities — no paid before any free.

    2026-07-05: chat:fast/smart/code joined this list — paid (deepseek/
    anthropic/openai) used to sit ahead of github/sambanova/zai "for
    backfill speed", meaning a paid call fired the moment the first ~5 free
    providers were saturated even though more free providers were still
    untried further down the chain. Explicit choice: slow-but-free beats
    fast-but-paid.

    2026-07-17: chat:smart LEFT this list — see
    test_chat_smart_is_deepseek_first_by_owner_choice."""
    chain = chain_for(capability)
    paid_idx = [i for i, p in enumerate(chain) if p in KNOWN_PAID]
    free_idx = [i for i, p in enumerate(chain) if p in KNOWN_FREE]
    if paid_idx and free_idx:
        assert max(free_idx) < min(paid_idx), (
            f"{capability}: free providers must precede all paid"
        )


def test_chat_smart_is_deepseek_first_by_owner_choice():
    """2026-07-17, owner-approved (cap raised $0.50→$1 for it): chat:smart is
    Stepan's money lane — quality beats price. deepseek v4-flash leads so every
    sales reply comes from ONE strong model with a warm per-account prompt
    cache (cache-hit input $0.0028/M ≈ $0.0004/reply) instead of whichever
    free key happens to be uncooled. The free pool stays as the fallback tail
    (deepseek flake and the $1 cap budget-downgrade both walk over to it).
    Also pre-positions the lane for cerebras' free-tier death 2026-08-17."""
    chain = chain_for("chat:smart")
    assert chain[0] == "deepseek"
    # the free fallback tail must survive right behind it
    assert {"groq", "gemini", "mistral"} <= set(chain[1:])
    # and the emergency paid quality tail stays at the very end
    assert chain[-2:] == ["anthropic", "openai"]


@pytest.mark.parametrize("capability", ["chat:fast", "chat:code"])
def test_chat_first_3_are_free(capability):
    """Documented invariant: chat:fast/code chains always START with at least 3
    free providers (chat:smart is deepseek-first by owner choice, see above)."""
    chain = chain_for(capability)
    for provider in chain[:3]:
        assert provider in KNOWN_FREE, (
            f"{capability}: first 3 must be free, got {provider}"
        )


@pytest.mark.parametrize("capability", ["chat:fast", "chat:smart"])
def test_paid_tail_present_for_interactive_chat(capability):
    """The guaranteed-answer tail: every interactive chat chain must keep at
    least one provider whose keys are typically paid, so a fully-saturated free
    pool still ends in an answer instead of a 503. deepseek is that anchor —
    if it's ever removed from a chain, this test forces a conscious
    replacement, not a silent free-only chain."""
    assert "deepseek" in chain_for(capability)


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


def test_transcription_has_gemini_fallback():
    """REGRESSION (2026-07-11): transcription was groq-only in practice (openai
    has no key), so when groq's free daily cap parked every key (~9h), voice had
    zero capacity. gemini (chat-based audio, separate quota) is now a fallback."""
    chain = chain_for("transcription")
    assert "groq" in chain
    assert "gemini" in chain


def test_transcription_local_asr_first():
    """2026-07-18: self-hosted faster-whisper (vera3's asr-local) goes first —
    free, private, no external rate limit. groq/gemini/openai stay as fallback
    for when ASR_LOCAL_URL is unset or the service is unreachable."""
    chain = chain_for("transcription")
    assert chain[0] == "local"
    assert chain.index("local") < chain.index("groq")


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


def test_deprioritize_for_json_never_drops_an_unreliable_provider():
    """An all-UNRELIABLE chain keeps every provider — a maybe-malformed retry
    still beats a 503. (Only JSON_INCAPABLE_PROVIDERS are dropped: their JSON
    is certainly-malformed, so keeping them never helps.)"""
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


def test_deprioritize_for_json_excludes_zai_entirely():
    """2026-07-05: zai/glm-4.5-flash doesn't support response_format at all
    (confirmed via litellm.get_supported_openai_params) — drop_params=True
    silently strips it, so the model never gets told to emit JSON: a
    100%-guaranteed InvalidJSON. 2026-07-16: deprioritizing wasn't enough
    (measured 44 InvalidJSON/45min as JSON traffic overflowed to the tail) —
    zai is now EXCLUDED from JSON chains, not just demoted."""
    assert "zai" in JSON_INCAPABLE_PROVIDERS
    assert "zai" not in JSON_UNRELIABLE_PROVIDERS
    assert deprioritize_for_json(["zai", "gemini"]) == ["gemini"]


def test_json_request_drops_zai_but_plain_text_keeps_it():
    """zai still serves plain-text chat:fast (it's a fine free model there);
    only the JSON-shaped effective chain loses it."""
    raw = chain_for("chat:fast")
    assert "zai" in raw
    assert "zai" not in deprioritize_for_json(raw)


def test_prefilter_chain_excludes_zai():
    """prefilter requests are always JSON — zai in that chain was a guaranteed
    billed-but-unusable call (2026-07-16)."""
    assert "zai" not in chain_for("prefilter")


def test_usable_scopes_anthropic_excludes_audio_and_vision():
    """REGRESSION (2026-07-15): the operator scoped the anthropic key to
    vision+audio expecting it to serve images and voice. Claude has NO
    speech-to-text (no transcription model at all), and anthropic was dropped
    from the vision chain after 400-ing on image URLs — so both scopes were
    inert and the key silently served only Coach. A scope is usable only if the
    provider is BOTH chained for the capability and has a model for it."""
    from aibroker.routing.chains import usable_scopes_for_provider
    assert usable_scopes_for_provider("anthropic") == frozenset({"llm:chat", "llm:edit"})


def test_usable_scopes_match_chain_and_model_for_key_providers():
    from aibroker.routing.chains import usable_scopes_for_provider
    assert "llm:audio" in usable_scopes_for_provider("groq")        # whisper, chained
    assert usable_scopes_for_provider("voyage") == frozenset({"llm:embed"})
    assert usable_scopes_for_provider("nvidia") == frozenset({"llm:deep"})
    # gemini is the multimodal workhorse — every lane it's chained for
    assert usable_scopes_for_provider("gemini") >= {"llm:chat", "llm:vision", "llm:audio"}
    assert usable_scopes_for_provider("nope-not-real") == frozenset()
