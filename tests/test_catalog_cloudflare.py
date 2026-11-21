"""Cloudflare Workers AI catalog refresh (2026-11-21): pin-only additions, llava removed."""
import pytest

from aibroker.providers.catalog import resolve_pin
from aibroker.providers.provider_specs.cloudflare import SPEC
from aibroker.routing.chains import CAPABILITY_CHAINS

PRICES = {
    "@cf/google/gemma-4-26b-a4b-it": (0.10, 0.30),
    "@cf/openai/gpt-oss-20b": (0.20, 0.30),
    "@cf/thinkingmachines/inkling-2-small": (0.30, 1.00),
    "@cf/qwen/qwen3.8-27b": (0.45, 3.20),
    "@cf/zai-org/glm-5.3": (1.40, 4.40),
    "@cf/deepseek-ai/deepseek-v4.1-flash-1017": (0.30, 1.20),
}
PIN_ONLY = [m for m in PRICES if m != "@cf/google/gemma-4-26b-a4b-it"]


@pytest.mark.parametrize("short", PRICES)
def test_added_model_resolves(short):
    targets = resolve_pin(short, "chat:fast", CAPABILITY_CHAINS["chat:fast"])
    assert ("cloudflare", f"cloudflare/{short}") in [(t.provider, t.model) for t in targets]


@pytest.mark.parametrize("short", PIN_ONLY)
def test_pin_only_not_in_defaults_or_rotation(short):
    mid = f"cloudflare/{short}"
    assert mid not in SPEC.defaults.values()
    assert all(mid not in r for r in SPEC.rotation.values())


@pytest.mark.parametrize("short,price", PRICES.items())
def test_price(short, price):
    from aibroker.providers.cost import estimate_llm_cost
    assert estimate_llm_cost(f"cloudflare/{short}", 10**6, 10**6) == pytest.approx(sum(price))


def test_llava_removed_and_vision_default_replaced():
    assert "cloudflare/@cf/llava-hf/llava-1.5-7b-hf" not in SPEC.models
    assert SPEC.defaults["vision"] == "cloudflare/@cf/google/gemma-4-26b-a4b-it"
