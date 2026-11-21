"""Catalog refresh 2026-11-21: pin-only anthropic and openai text models.

Sources: platform.claude.com/docs/en/about-claude/{models/overview,pricing},
developers.openai.com/api/docs/{models,pricing,deprecations}."""
from __future__ import annotations

import litellm
import pytest

from aibroker.providers.catalog import resolve_pin
from aibroker.providers.registry import model_spec
from aibroker.providers.registry import spec_or_default as spec
from aibroker.routing.chains import CAPABILITY_CHAINS

# routing id -> (input, output) USD per 1M tokens
ADDED = {
    "anthropic/claude-fable-5-2": (8.0, 40.0),
    "anthropic/claude-opus-5-5": (4.0, 20.0),
    "anthropic/claude-sonnet-5-5": (2.0, 10.0),
    "anthropic/claude-haiku-5-5": (0.10, 0.50),
    "openai/gpt-6.2-astra": (8.0, 40.0),
    "openai/gpt-6.2-sol": (2.0, 10.0),
    "openai/gpt-6.1-terra": (1.0, 5.0),
    "openai/gpt-6-luna": (0.10, 0.50),
}


@pytest.mark.parametrize("rid", ADDED)
def test_added_model_resolves_for_structured_pin(rid):
    provider, short = rid.split("/", 1)
    targets = resolve_pin(short, "structured", CAPABILITY_CHAINS["structured"])
    assert [(t.provider, t.model) for t in targets] == [(provider, rid)]


@pytest.mark.parametrize("rid", ADDED)
def test_added_model_is_pin_only_and_priced(rid):
    provider, short = rid.split("/", 1)
    ps = spec(provider)
    assert rid not in ps.defaults.values()
    assert all(rid not in chain for chain in ps.rotation.values())
    m = model_spec(rid)
    assert m is not None and m.pricing == "litellm"
    cost = litellm.model_cost[short]
    inp, out = ADDED[rid]
    assert cost["input_cost_per_token"] * 1e6 == pytest.approx(inp)
    assert cost["output_cost_per_token"] * 1e6 == pytest.approx(out)


def test_schema_flag_only_where_documented():
    assert model_spec("anthropic/claude-haiku-5-5").litellm_extra == {"supports_response_schema": True}
    assert model_spec("openai/gpt-6.2-astra").litellm_extra == {"supports_response_schema": True}
    assert not model_spec("openai/gpt-6.2-sol").litellm_extra


def test_openai_pins_read_images():
    assert "vision" in model_spec("openai/gpt-6.1-terra").capabilities


def test_existing_defaults_unchanged():
    assert spec("anthropic").defaults["chat:smart"] == "anthropic/claude-sonnet-5"
    assert spec("openai").defaults["chat:smart"] == "openai/gpt-5"
    assert spec("openai").defaults["chat:fast"] == "openai/gpt-5-mini"
