"""Provider registry consistency: one ProviderSpec per provider is the ONLY place
provider facts live, so these invariants are what make "add a provider = one
entry" safe."""
from __future__ import annotations

import pytest

from aibroker.providers import registry
from aibroker.providers.registry import (
    REGISTRY,
    ProbeSpec,
    ProviderSpec,
    all_models,
    build_provider,
    model_spec,
    spec_or_default,
)
from aibroker.routing.chains import CAPABILITY_CHAINS, CAPABILITY_SCOPE

# --- chains <-> registry ------------------------------------------------------


def test_every_chain_provider_has_a_spec():
    for cap, chain in CAPABILITY_CHAINS.items():
        for provider in chain:
            assert provider in REGISTRY, f"{provider} is chained in {cap} but has no ProviderSpec"


def test_every_chain_pair_resolves_to_a_default_model():
    for cap, chain in CAPABILITY_CHAINS.items():
        for provider in chain:
            assert registry.model_for(provider, cap), (
                f"{provider} is chained in {cap} but has no default model there")


def test_every_default_and_rotation_model_belongs_to_its_provider():
    for name, spec in REGISTRY.items():
        for cap, model in spec.defaults.items():
            assert model_spec(model) is not None and model_spec(model).provider == name, (name, cap, model)
        for cap, models in spec.rotation.items():
            for m in models:
                assert model_spec(m).provider == name, (name, cap, m)
        for cap in spec.defaults:
            assert cap in CAPABILITY_SCOPE, f"{name}: unknown capability {cap}"


def test_model_ids_are_unique_across_providers():
    ids = [m.id for m in all_models()]
    assert len(ids) == len(set(ids))


# --- pricing: priced, or explicitly free/local --------------------------------


def test_every_model_is_priced_or_explicitly_marked():
    """A model nobody priced silently books $0 and blinds every daily cap. Each
    ModelSpec is litellm-priced (verified here against litellm's own map), has an
    explicit override / per-minute price, or is marked free / local."""
    import litellm

    from aibroker.providers import cost  # noqa: F401 — registers the overrides

    unpriced = []
    for m in all_models():
        if m.pricing == "litellm":
            try:
                info = litellm.get_model_info(m.id)
                assert info.get("input_cost_per_token") is not None
            except Exception:  # noqa: BLE001 — not in litellm's map
                unpriced.append(m.id)
        elif m.pricing == "override":
            assert m.input_usd_per_mtok is not None and m.output_usd_per_mtok is not None, m.id
        elif m.pricing == "per_minute":
            assert m.usd_per_minute and m.usd_per_minute > 0, m.id
        else:
            assert m.pricing in ("free", "local"), m.id
    assert not unpriced, (
        "models litellm cannot price — give each an explicit price or mark it "
        f"pricing='free'/'local' in providers/specs.py: {unpriced}")


def test_registered_overrides_reach_litellm():
    import litellm

    from aibroker.providers import cost  # noqa: F401

    assert litellm.model_cost["voyage/voyage-4"]["input_cost_per_token"] == pytest.approx(6e-8)
    assert litellm.model_cost["deepseek/deepseek-flash"]["cache_read_input_token_cost"] == pytest.approx(3e-9)


def test_local_models_are_marked_local_and_use_a_raw_transport():
    for mid, transport in (("local/whisper", "local_asr"), ("local/qwen3vl", "local_vision")):
        m = model_spec(mid)
        assert m.pricing == "local" and m.transport == transport


# --- transports ---------------------------------------------------------------


def test_every_model_transport_exists_and_implements_its_protocol():
    from aibroker.providers import transport as t

    table = t._load()
    for m in all_models():
        assert m.transport in table, (m.id, m.transport)
    assert isinstance(table["litellm"], t.ChatTransport)
    assert isinstance(table["litellm"], t.EmbedTransport)
    assert isinstance(table["litellm"], t.TranscribeTransport)
    assert isinstance(table["local_vision"], t.ChatTransport)
    assert isinstance(table["local_asr"], t.TranscribeTransport)
    assert isinstance(table["gemini_asr"], t.TranscribeTransport)
    assert isinstance(table["openrouter_decisions"], t.DecideTransport)


def test_transport_for_unknown_model_defaults_to_litellm():
    from aibroker.providers.transport import transport_for

    assert type(transport_for("gemini/some-pinned-thing")).__name__ == "LiteLLMTransport"
    assert type(transport_for("local/qwen3vl")).__name__ == "LocalVisionTransport"
    assert type(transport_for("gemini/gemini-3.5-transcribe")).__name__ == "GeminiAsrTransport"


# --- the views reproduce the old tables ---------------------------------------


def test_views_match_the_tables_they_replaced():
    assert registry.paid_providers() == {"deepseek", "anthropic", "openai"}
    assert registry.cache_sticky_providers() == {"deepseek", "anthropic"}
    assert registry.providers_with_json("unreliable") == {"cerebras", "cohere", "openrouter", "groq"}
    assert registry.providers_with_json("incapable") == {"zai"}
    assert registry.max_keys_for("gemini") == registry.max_keys_for("cerebras") == 3
    assert registry.max_keys_for("groq") == 5 and registry.max_keys_for("unknown") == 5
    assert registry.cooldown_base_for("mistral") == 10
    assert registry.cooldown_base_for("local") == 30
    assert registry.cooldown_base_for("nonexistent") == 300
    assert REGISTRY["groq"].max_request_tokens == 8_000
    assert REGISTRY["cerebras"].max_request_tokens is None


def test_documented_cache_key_params_only():
    """A stable cache key is sent ONLY where the provider documents a parameter."""
    assert {n: s.cache_key_param for n, s in REGISTRY.items() if s.cache_key_param} == {
        "openai": "prompt_cache_key", "mistral": "prompt_cache_key",
        "cerebras": "prompt_cache_key", "openrouter": "session_id"}
    assert [n for n, s in REGISTRY.items() if s.explicit_cache] == ["anthropic"]


def test_provider_ranks_are_unique_and_paid_rank_last():
    ranks = [s.rank for s in REGISTRY.values()]
    assert len(ranks) == len(set(ranks)), "ties would make pinned order ambiguous"
    paid_min = min(s.rank for s in REGISTRY.values() if s.paid)
    free_chat = [s.rank for n, s in REGISTRY.items() if not s.paid and n != "voyage"]
    assert max(free_chat) < paid_min


def test_unknown_provider_gets_neutral_defaults():
    spec = spec_or_default("nope")
    assert spec.json_reliability == "reliable" and not spec.paid and spec.probe is None
    assert registry.model_for("nope", "chat:fast") is None


def test_every_probe_builds_a_request():
    for name, spec in REGISTRY.items():
        if spec.probe is None:
            continue
        req = spec.probe.build("KEY", "acct" if spec.probe.needs_account_id else None)
        assert req is not None, name
        method, url, headers, body = req
        assert method in ("POST", "GET") and url.startswith("https://") and "{" not in url
        assert (method == "GET") == (body is None)
        assert any("KEY" in v for v in headers.values()), name


def test_probe_without_required_account_is_unprobeable():
    assert REGISTRY["cloudflare"].probe.build("k", None) is None


def test_duplicate_registration_is_refused():
    with pytest.raises(ValueError, match="registered twice"):
        registry.register(build_provider("groq"))


# --- adding a provider really is one entry ------------------------------------


def test_a_new_provider_is_one_registry_entry(monkeypatch):
    """The point of the refactor: a registered spec alone is enough for the views,
    the catalog and the pin resolver to see the provider and its model."""
    from aibroker.providers.catalog import resolve_pin

    monkeypatch.setitem(REGISTRY, "acme", build_provider(
        "acme", rank=15, cooldown_base_s=45, max_keys=2, json_reliability="unreliable",
        defaults={"chat:fast": "acme/turbo-1"},
        model_meta={"acme/turbo-1": {"pricing": "free"}}))
    assert registry.max_keys_for("acme") == 2
    assert "acme" in registry.providers_with_json("unreliable")
    assert model_spec("acme/turbo-1").provider == "acme"
    assert [t.provider for t in resolve_pin("turbo-1", "chat:fast", ["acme"])] == ["acme"]
    assert isinstance(REGISTRY["acme"], ProviderSpec) and isinstance(ProbeSpec("u", {}), ProbeSpec)
