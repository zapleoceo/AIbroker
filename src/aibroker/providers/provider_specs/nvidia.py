"""Definition for the nvidia provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

SPEC = build_provider(
    "nvidia", rank=100, cooldown_base_s=300,
    quota=Quota(doc="https://build.nvidia.com/settings/api-keys"),
    probe=chat_probe("https://integrate.api.nvidia.com/v1/chat/completions",
                      "nvidia/nemotron-3-ultra-550b-a55b"),
    defaults={"chat:deep": "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b"},
    model_meta={"nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b": {
        "pricing": "free", "note": "no litellm price; credits-based free tier",
        "litellm_extra": {"litellm_provider": "nvidia_nim"}}},
)
