"""Catalog refresh 2026-11-21: deepseek / zai / nvidia pin-only additions."""
from __future__ import annotations

import pytest

from aibroker.providers.catalog import resolve_pin
from aibroker.providers.registry import get_spec, model_spec
from aibroker.routing.chains import CAPABILITY_CHAINS

_NVIDIA = ("moonshotai/kimi-k3.1-pro", "mistralai/mistral-large-4-1t-a52b",
           "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3.5-lightning-30b-a3b",
           "google/gemma-4-31b-it", "minimaxai/minimax-m3.1-flash")


def _unlisted(provider: str, mid: str) -> None:
    spec = get_spec(provider)
    assert mid not in spec.defaults.values()
    assert all(mid not in r for r in spec.rotation.values())


def test_deepseek_v4_pro_pin():
    m = model_spec("deepseek/deepseek-v4-pro")
    assert (m.pricing, m.input_usd_per_mtok, m.output_usd_per_mtok) == ("override", 0.66, 1.98)
    assert m.cache_read_usd_per_mtok == 0.022
    _unlisted("deepseek", m.id)
    for cap in ("chat:smart", "structured"):
        assert resolve_pin("deepseek-v4-pro", cap, ["deepseek"])[0].model == m.id


def test_zai_default_replaced():
    spec = get_spec("zai")
    assert spec.defaults["chat:fast"] == "zai/glm-5.3-flash"
    assert spec.defaults["prefilter"] == "zai/glm-5.3-flash"
    assert model_spec("zai/glm-5.3-flash").pricing == "free"
    assert model_spec("zai/glm-4.7-flash") is None
    assert resolve_pin("glm-5.3-flash", "chat:fast", CAPABILITY_CHAINS["chat:fast"])


@pytest.mark.parametrize("mid", _NVIDIA)
def test_nvidia_pins(mid):
    full = f"nvidia_nim/{mid}"
    m = model_spec(full)
    assert m.pricing == "free"
    _unlisted("nvidia", full)
    assert resolve_pin(mid, "chat:deep", CAPABILITY_CHAINS["chat:deep"])[0].model == full
