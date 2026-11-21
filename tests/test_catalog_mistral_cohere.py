"""Mistral + Cohere catalog refresh (2026-11-21): pin-only additions, mistral-large gone."""
import pytest

from aibroker.providers.catalog import resolve_pin
from aibroker.providers.cost import estimate_llm_cost
from aibroker.providers.provider_specs.cohere import SPEC as COHERE
from aibroker.providers.provider_specs.mistral import SPEC as MISTRAL
from aibroker.routing.chains import CAPABILITY_CHAINS

# Neither provider is chained on `structured` yet (chains.py is outside this refresh),
# so the pin resolver is exercised with them appended to the chain.
CHAIN = [*CAPABILITY_CHAINS["structured"], "mistral", "cohere"]

PRICES = {
    "mistral/ministral-14b-latest": ("mistral", 0.20, 0.20),
    "mistral/ministral-3b-latest": ("mistral", 0.10, 0.10),
    "cohere/command-a-plus-05-2026": ("cohere", 0.30, 1.50),
    "cohere/command-a-03-2025": ("cohere", 2.50, 10.00),
}
SPECS = {"mistral": MISTRAL, "cohere": COHERE}


@pytest.mark.parametrize("mid", PRICES)
def test_added_model_resolves(mid):
    provider = PRICES[mid][0]
    targets = resolve_pin(mid.split("/", 1)[1], "structured", CHAIN)
    assert (provider, mid) in [(t.provider, t.model) for t in targets]


@pytest.mark.parametrize("mid", PRICES)
def test_pin_only_not_in_defaults_or_rotation(mid):
    spec = SPECS[PRICES[mid][0]]
    assert mid not in spec.defaults.values()
    assert all(mid not in r for r in spec.rotation.values())


@pytest.mark.parametrize("mid,price", PRICES.items())
def test_price(mid, price):
    assert estimate_llm_cost(mid, 10**6, 10**6) == pytest.approx(price[1] + price[2])


def test_large_removed_and_smart_default_replaced():
    assert "mistral/mistral-large-latest" not in MISTRAL.models
    assert MISTRAL.defaults["chat:smart"] == "mistral/mistral-medium-latest"
    assert estimate_llm_cost("mistral/mistral-medium-latest", 10**6, 10**6) == pytest.approx(9.0)


def test_other_defaults_unchanged():
    assert MISTRAL.defaults["structured"] == "mistral/mistral-small-latest"
    assert COHERE.defaults["structured"] == "cohere/command-r7b-12-2024"
