"""Newest Gemini text models: pin-only on chat:* / structured, never walked unpinned."""
from __future__ import annotations

import litellm
import pytest

import aibroker.providers.cost  # noqa: F401  (registers override prices)
from aibroker.providers.catalog import UnknownModel, resolve_pin
from aibroker.providers.model_scores import SCORES
from aibroker.providers.provider_specs.gemini import SPEC
from aibroker.providers.registry import model_spec
from aibroker.routing.chains import CAPABILITY_CHAINS

PINNED = {  # id -> (input, output, cache read) USD per 1M tokens
    "gemini/gemini-4-neon": (3.00, 15.00, 0.15),
    "gemini/gemini-4-carbon": (2.00, 10.00, 0.10),
    "gemini/gemini-4-hydrogen": (1.50, 7.50, 0.075),
    "gemini/gemini-4-argon": (2.00, 10.00, 0.10),
    "gemini/gemini-3.8-flash": (0.75, 3.75, 0.075),
}


@pytest.mark.parametrize("model", sorted(PINNED))
@pytest.mark.parametrize("capability", ["structured", "chat:smart", "chat:fast", "chat:sales"])
def test_pin_resolves_on_text_lanes(model, capability):
    short = model.split("/", 1)[1]
    targets = resolve_pin(short, capability, CAPABILITY_CHAINS[capability])
    assert [(t.provider, t.model) for t in targets] == [("gemini", model)]


@pytest.mark.parametrize("model", sorted(PINNED))
def test_never_in_default_walk(model):
    assert model not in SPEC.defaults.values()
    assert all(model not in models for models in SPEC.rotation.values())


@pytest.mark.parametrize("model", sorted(PINNED))
def test_not_pinnable_on_media_lanes(model):
    short = model.split("/", 1)[1]
    for capability in ("vision", "image", "transcription"):
        with pytest.raises(UnknownModel):
            resolve_pin(short, capability, CAPABILITY_CHAINS[capability])


@pytest.mark.parametrize(("model", "prices"), sorted(PINNED.items()))
def test_override_prices_and_json_schema(model, prices):
    spec = model_spec(model)
    assert spec.pricing == "override"
    assert (spec.input_usd_per_mtok, spec.output_usd_per_mtok,
            spec.cache_read_usd_per_mtok) == prices
    entry = litellm.model_cost[model]
    assert entry["input_cost_per_token"] == pytest.approx(prices[0] / 1e6)
    assert entry["output_cost_per_token"] == pytest.approx(prices[1] / 1e6)
    assert entry["supports_response_schema"] is True


def test_gemini_37_flash_is_not_registered():
    # Google serves gemini-3.7-flash as gemini-3.8-flash; pin the real one.
    assert model_spec("gemini/gemini-3.7-flash") is None


def test_gemini_4_scores_and_38_unscored():
    assert {m: SCORES[m].value for m in ("gemini-4-neon", "gemini-4-carbon",
                                         "gemini-4-hydrogen", "gemini-4-argon")} == {
        "gemini-4-neon": 61.0, "gemini-4-carbon": 60.0,
        "gemini-4-hydrogen": 54.0, "gemini-4-argon": 50.0}
    assert "gemini-3.8-flash" not in SCORES


def test_pinned_chat_model_resolves_on_structured_outside_its_walk():
    from aibroker.routing.chains import chain_for, pin_chain_for

    # The unpinned structured walk is unchanged ...
    assert "openrouter" not in chain_for("structured")
    # ... but an explicit pin of a chat-family model of any chat-lane provider is served.
    assert pin_chain_for("structured")[:len(chain_for("structured"))] == chain_for("structured")
    for provider in ("openrouter", "deepseek", "sambanova", "cerebras", "nvidia"):
        assert provider in pin_chain_for("structured")
    targets = resolve_pin("openrouter/moonshotai/kimi-k3", "structured", pin_chain_for("structured"))
    assert [t.provider for t in targets] == ["openrouter"]
    # Non-chat lanes keep their own chain only.
    assert pin_chain_for("vision") == chain_for("vision")
