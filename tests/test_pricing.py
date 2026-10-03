"""Pricing regression: every routable model must be priced (or explicitly
free/local), and the cost paths added with the 1.103 upgrade stay correct."""
from __future__ import annotations

import importlib.metadata
import pathlib
import re
from types import SimpleNamespace

import pytest

from aibroker.providers import cost as a
from aibroker.providers import litellm_client
from aibroker.providers.pricing import (
    audio_surcharge,
    is_priced,
    reported_cost,
)
from aibroker.providers.registry import all_models, model_spec


def _routable_models() -> set[str]:
    return {m.id for m in all_models() if m.capabilities}


@pytest.mark.parametrize("model", sorted(_routable_models()))
def test_every_routable_model_has_a_price(model):
    """Adding a model to a ProviderSpec without a price (LiteLLM
    map, a register_model entry, or the per-minute ASR table) blinds the cost
    caps for it. Free models must be registered at 0.0, not left out."""
    spec = model_spec(model)
    if spec.pricing in ("local", "per_minute"):
        return  # self-hosted, or priced per audio minute (ModelSpec.usd_per_minute)
    assert is_priced(model), f"{model} has no pricing - register it (see docs/pricing.md)"


def test_litellm_pin_matches_installed_version():
    pin = re.search(r"litellm==([\w.]+)", pathlib.Path("pyproject.toml").read_text()).group(1)
    assert importlib.metadata.version("litellm") == pin


def test_sonnet5_is_permanently_2_and_10_per_million():
    assert a.estimate_llm_cost("anthropic/claude-sonnet-5", 1_000_000, 1_000_000)         == pytest.approx(12.0)


def test_registered_overrides():
    assert a.estimate_llm_cost("cloudflare/@cf/openai/gpt-oss-120b", 1_000_000, 1_000_000)         == pytest.approx(0.35 + 0.75)
    assert a.estimate_llm_cost("nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b", 10**6, 10**6) == 0.0
    assert is_priced("cloudflare/@cf/llava-hf/llava-1.5-7b-hf")


def test_groq_whisper_bills_ten_second_minimum():
    one = a.whisper_cost("groq/whisper-large-v3-turbo", 1.0)
    assert one == pytest.approx(a.whisper_cost("groq/whisper-large-v3-turbo", 10.0))
    assert a.whisper_cost("groq/whisper-large-v3-turbo", 30.0) == pytest.approx(3 * one)
    # no floor for other providers
    assert a.whisper_cost("openai/whisper-1", 1.0) == pytest.approx(0.006 / 60)


def test_gemini_transcribe_rate_is_input_plus_output_per_minute():
    # $0.003/min audio in + $0.002/min text out (ai.google.dev pricing, 2026-10-03)
    assert model_spec("gemini/gemini-3.5-transcribe").usd_per_minute == pytest.approx(0.003 + 0.002)


def test_audio_tokens_priced_at_audio_rate_from_override_table():
    m = "gemini/gemini-2.5-flash"  # text $0.30/M, audio $1.00/M
    plain = a.estimate_llm_cost(m, 100_000, 0)
    audio = a.estimate_llm_cost(m, 100_000, 0, audio_input_tokens=100_000)
    assert audio - plain == pytest.approx(100_000 * (1.00 - 0.30) / 1e6)


def test_audio_surcharge_reads_litellm_map_and_ignores_unknown():
    assert audio_surcharge("gemini/gemini-3.5-flash", 1000) == 0.0  # audio == text rate
    assert audio_surcharge("gemini/gemini-3-flash-preview", 1_000_000) == pytest.approx(0.5)
    assert audio_surcharge("nope/unknown", 1000) == 0.0
    assert audio_surcharge("gemini/gemini-2.5-flash", 0) == 0.0


def test_reported_cost_shapes():
    assert reported_cost({"cost": 0.0123}) == 0.0123
    assert reported_cost(SimpleNamespace(cost=0)) == 0.0
    assert reported_cost({}) is None
    assert reported_cost(SimpleNamespace()) is None
    assert reported_cost({"cost": "x"}) is None
    assert reported_cost({"cost": True}) is None


def test_prefer_reported_cost_only_for_openrouter():
    assert litellm_client._prefer_reported_cost("openrouter/x/y", {"cost": 0.5}, 0.1) == 0.5
    assert litellm_client._prefer_reported_cost("openrouter/x/y", {}, 0.1) == 0.1
    assert litellm_client._prefer_reported_cost("groq/x", {"cost": 0.5}, 0.1) == 0.1
