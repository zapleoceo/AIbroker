"""Definition for the nvidia provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

# In the trial key's /v1/models on 2026-11-21; not in defaults/rotation.
_PINNED = ("moonshotai/kimi-k3.1-pro", "mistralai/mistral-large-4-1t-a52b",
           "nvidia/nemotron-3.5-super-vl-120b-a12b", "nvidia/nemotron-3.5-lightning-30b-a3b",
           "google/gemma-4-31b-it", "minimaxai/minimax-m3.1-flash")

SPEC = build_provider(
    "nvidia", rank=100, cooldown_base_s=300,
    quota=Quota(doc="https://build.nvidia.com/settings/api-keys"),
    probe=chat_probe("https://integrate.api.nvidia.com/v1/chat/completions",
                      "nvidia/nemotron-3-ultra-550b-a55b"),
    defaults={"chat:deep": "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"},
    model_meta={"nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b": {
        "pricing": "free", "note": "no litellm price; credits-based free tier",
        "litellm_extra": {"litellm_provider": "nvidia_nim"}},
        **{f"nvidia_nim/{m}": {   # pin-only: newest chat/reasoning NIMs on the trial key
            "capabilities": ("chat:deep",), "pricing": "free",
            "note": "no litellm price; credits-based free tier; pin-only",
            "litellm_extra": {"litellm_provider": "nvidia_nim"}}
           for m in _PINNED}},
)
