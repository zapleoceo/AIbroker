"""Definition for the deepseek provider."""
from __future__ import annotations

from aibroker.providers.adapters import _DeepseekAdapter
from aibroker.providers.provider_specs.common import CHAT_USER_PING
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

SPEC = build_provider(
    "deepseek", rank=200, paid=True, cache_sticky=True, cooldown_base_s=30,
    adapter=_DeepseekAdapter(),
    rate_limit_signs=("response_format type is unavailable",),
    quota_headers="openai", quota=Quota(doc="https://api-docs.deepseek.com/quick_start/pricing"),
    probe=ProbeSpec("https://api.deepseek.com/chat/completions",
                    {"model": "deepseek-flash", "messages": CHAT_USER_PING,
                     "max_tokens": 1, "thinking": {"type": "disabled"}}),
    defaults={"chat:fast": "deepseek/deepseek-flash", "chat:smart": "deepseek/deepseek-flash",
              "chat:sales": "deepseek/deepseek-flash", "chat:edit": "deepseek/deepseek-flash",
              "chat:code": "deepseek/deepseek-flash", "vision": "deepseek/deepseek-flash"},
    model_meta={"deepseek/deepseek-flash": {   # absent from litellm: OFF-PEAK list prices
        "pricing": "override", "input_usd_per_mtok": 0.15,
        "cache_read_usd_per_mtok": 0.003, "output_usd_per_mtok": 0.60,
        "litellm_extra": {
            "max_input_tokens": 1_000_000, "max_output_tokens": 384_000,
            "litellm_provider": "deepseek", "mode": "chat", "supports_vision": True,
            "supports_response_schema": True, "supports_prompt_caching": True}},
        # Pin-only (never in defaults/rotation). Upstream /models lists it today
        # (text-only input, 1M context). Off-peak list prices per 1M tokens from
        # https://api-docs.deepseek.com/quick_start/pricing (2026-11-21); peak is 2x
        # (peak_multiplier). litellm's map carries the PEAK price, so override.
        # json_schema is downgraded to json_object by _DeepseekAdapter.
        "deepseek/deepseek-v4-pro": {
            "capabilities": ("chat:smart", "chat:sales", "chat:code", "chat:edit", "structured"),
            "pricing": "override", "input_usd_per_mtok": 0.66,
            "cache_read_usd_per_mtok": 0.022, "output_usd_per_mtok": 1.98,
            "note": "DeepSeek-V4-Pro; pin-only; text input only",
            "litellm_extra": {
                "max_input_tokens": 1_048_576, "max_output_tokens": 393_216,
                "litellm_provider": "deepseek", "mode": "chat",
                "supports_response_schema": True, "supports_prompt_caching": True}}},
)
