"""Catalog refresh 2026-11-21: groq / cerebras / sambanova pin-only text models."""
from __future__ import annotations

import pytest

from aibroker.providers.catalog import resolve_pin
from aibroker.providers.registry import get_spec, model_spec
from aibroker.routing.chains import CAPABILITY_CHAINS

# (short id, routing id, input $/Mtok, output $/Mtok, json_schema)
ADDED = [
    ("openai/gpt-oss-20b", "groq/openai/gpt-oss-20b", 0.075, 0.30, True),
    ("qwen/qwen3.8-27b", "groq/qwen/qwen3.8-27b", 0.80, 4.00, True),
    ("qwen-3.8-27b", "cerebras/qwen-3.8-27b", 0.99, 1.49, True),
    ("GLM-5.3", "sambanova/GLM-5.3", 2.80, 8.80, False),
    ("MiniMax-M3.1-Flash", "sambanova/MiniMax-M3.1-Flash", 0.30, 1.20, False),
]


@pytest.mark.parametrize("short,rid,inp,out,schema", ADDED)
def test_added_model_is_pin_only_and_priced(short, rid, inp, out, schema):
    spec = model_spec(rid)
    assert spec is not None
    provider = get_spec(spec.provider)
    assert rid not in provider.defaults.values()
    assert all(rid not in models for models in provider.rotation.values())
    # cerebras is not in the structured chain (only groq/gemini/anthropic/openai), so
    # it is checked on chat:fast; sambanova models on chat:smart.
    lane = {"groq": "structured", "cerebras": "chat:fast",
            "sambanova": "chat:smart"}[spec.provider]
    targets = resolve_pin(short, lane, CAPABILITY_CHAINS[lane])
    assert rid in [t.model for t in targets]
    assert bool(spec.litellm_extra.get("supports_response_schema")) is schema
    if spec.pricing == "override":
        assert (spec.input_usd_per_mtok, spec.output_usd_per_mtok) == (inp, out)
    else:
        import litellm
        cost = litellm.model_cost[rid]
        assert cost["input_cost_per_token"] * 1e6 == pytest.approx(inp)
        assert cost["output_cost_per_token"] * 1e6 == pytest.approx(out)


def test_minimax_reads_images_only_on_sambanova():
    assert "vision" in model_spec("sambanova/MiniMax-M3.1-Flash").capabilities
    assert "vision" not in model_spec("sambanova/GLM-5.3").capabilities


def test_defaults_unchanged():
    assert get_spec("groq").defaults["structured"] == "groq/openai/gpt-oss-120b"
    assert get_spec("cerebras").defaults["structured"] == "cerebras/gpt-oss-120b"
    assert get_spec("sambanova").defaults["chat:smart"] == "sambanova/DeepSeek-V3.2"
