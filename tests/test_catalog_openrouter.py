"""OpenRouter catalog refresh (2026-11-21): new pin-only models, price fix."""
from __future__ import annotations

import pytest

from aibroker.providers.catalog import resolve_pin
from aibroker.providers.provider_specs.openrouter import SPEC
from aibroker.providers.registry import model_spec
from aibroker.routing.chains import pin_chain_for

PAID = {
    "anthropic/claude-fable-5.2": (8.0, 40.0),
    "anthropic/claude-sonnet-5.5": (2.0, 10.0),
    "anthropic/claude-haiku-5.5": (0.1, 0.5),
    "openai/gpt-6.2-sol": (2.0, 10.0),
    "google/gemini-4-hydrogen": (1.5, 7.5),
    "qwen/qwen4-max": (1.425, 4.275),
    "deepseek/deepseek-v4.1-flash-1017": (0.15, 0.6),
    "moonshotai/kimi-k3.1-pro": (0.253, 8.8),
    "z-ai/glm-5.3": (0.0699, 1.4),
    "mistralai/mistral-large-4-0": (0.7, 2.1),
}
FREE = ["inclusionai/ling-3.2-flash:free", "particle-ai/particle-1:free",
        "nvidia/nemotron-3.5-super-vl:free"]


def _pinned_ids():
    return [f"openrouter/{m}" for m in (*PAID, *FREE)]


@pytest.mark.parametrize("short", [*PAID, *FREE])
def test_added_model_resolves_when_pinned_on_structured(short):
    # openrouter is not in the `structured` walk; an explicit pin still reaches it.
    targets = resolve_pin(f"openrouter/{short}", "structured", pin_chain_for("structured"))
    assert [(t.provider, t.model) for t in targets] == [("openrouter", f"openrouter/{short}")]


@pytest.mark.parametrize("short,price", PAID.items())
def test_paid_price(short, price):
    spec = model_spec(f"openrouter/{short}")
    assert spec.pricing == "override"
    assert (spec.input_usd_per_mtok, spec.output_usd_per_mtok) == price


@pytest.mark.parametrize("short", FREE)
def test_free_models_priced_free(short):
    assert model_spec(f"openrouter/{short}").pricing == "free"


def test_pinned_models_not_in_defaults_or_rotation():
    wired = set(SPEC.defaults.values())
    for models in SPEC.rotation.values():
        wired.update(models)
    assert not wired & set(_pinned_ids())


def test_kimi_k3_price_fixed():
    spec = model_spec("openrouter/moonshotai/kimi-k3")
    assert (spec.input_usd_per_mtok, spec.output_usd_per_mtok) == (0.192, 16.0)
