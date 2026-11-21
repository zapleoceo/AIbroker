"""Definition for the cohere provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import CHAT_USER_PING
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

COHERE_CHAT_LANES = ("chat:fast", "chat:smart", "chat:code", "structured", "prefilter", "translate")

# Pin-only additions (catalog refresh 2026-11-21, trial key = free). cohere.com/pricing
# prints per-token rates only for legacy models, so: command-a-03-2025 uses litellm's map
# ($2.50/$10, same as OpenRouter's passthrough); command-a-plus-05-2026 is not in litellm,
# so the override is OpenRouter's passthrough rate ($0.30/$1.50, cache read $0.15).
# Upstream features list json_schema for both; command-a-plus also reads images.
_PIN_ONLY = {"capabilities": COHERE_CHAT_LANES,
             "litellm_extra": {"supports_response_schema": True}}

SPEC = build_provider(
    "cohere", rank=90, cooldown_base_s=60, json_reliability="unreliable",
    quota=Quota(req_per_day=1_000, doc="https://docs.cohere.com/v2/docs/rate-limits"),
    probe=ProbeSpec("https://api.cohere.com/v1/models?page_size=1"),
    billing_probe=ProbeSpec("https://api.cohere.com/v2/chat",
                            {"model": "command-r7b-12-2024", "max_tokens": 1,
                             "messages": CHAT_USER_PING}),
    defaults={"chat:fast": "cohere/command-r7b-12-2024",
              "chat:smart": "cohere/command-r7b-12-2024",
              "chat:code": "cohere/command-r7b-12-2024",
              "prefilter": "cohere/command-r7b-12-2024",
              "structured": "cohere/command-r7b-12-2024",
              "translate": "cohere/command-r7b-12-2024",
              "embedding": "cohere/embed-english-v3.0"},
    model_meta={
        "cohere/command-a-plus-05-2026": {
            **_PIN_ONLY, "pricing": "override", "input_usd_per_mtok": 0.30,
            "output_usd_per_mtok": 1.50, "cache_read_usd_per_mtok": 0.15,
            "note": "Command A+ (MoE, vision input, reasoning); price from OpenRouter"},
        "cohere/command-a-03-2025": {
            **_PIN_ONLY, "pricing": "litellm", "note": "Command A (256k ctx)"},
    },
)
