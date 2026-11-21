"""Definition for the mistral provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

MISTRAL_CHAT_LANES = ("chat:fast", "chat:smart", "chat:code", "structured", "prefilter", "translate")

# Pin-only additions (catalog refresh 2026-11-21, free-tier key). Mistral publishes no
# per-model price table on mistral.ai/pricing (only Large at $0.5/$1.5, which our key's
# model list no longer serves), so prices are litellm's map, which matches OpenRouter's
# passthrough rates for the same models. Ministral 3 reads images (upstream
# capabilities.vision) but the vision lane is not chained to mistral, so no "vision" cap.
# Docs list structured outputs for the whole current family.
_PIN_ONLY = {"capabilities": MISTRAL_CHAT_LANES, "pricing": "litellm",
             "litellm_extra": {"supports_response_schema": True}}

SPEC = build_provider(
    "mistral", rank=80, cooldown_base_s=10, native_tools=True,  # 1 RPS
    cache_key_param="prompt_cache_key",   # docs: docs.mistral.ai/api/endpoint/chat
    rate_limit_signs=("unauthorized",), monthly_signs=("unauthorized",),
    quota_headers="openai",               # bare 401 = monthly plan exhaustion (not a revoked key)
    quota=Quota(doc="https://docs.mistral.ai/deployment/laplateforme/tier/"),
    probe=ProbeSpec("https://api.mistral.ai/v1/models"),
    billing_probe=chat_probe("https://api.mistral.ai/v1/chat/completions", "mistral-small-latest"),
    defaults={"chat:fast": "mistral/mistral-small-latest",
              "chat:smart": "mistral/mistral-medium-latest",
              "chat:code": "mistral/codestral-latest",
              "prefilter": "mistral/mistral-small-latest",
              "structured": "mistral/mistral-small-latest",
              "translate": "mistral/mistral-small-latest"},
    # mistral-large-latest is gone from /v1/models (2026-11-21); chat:smart moved to
    # mistral-medium-latest (Medium 3.5, $1.5/$7.5 per 1M; the flagship alias that remains).
    model_meta={
        "mistral/mistral-medium-latest": {"pricing": "litellm"},
        "mistral/ministral-14b-latest": {**_PIN_ONLY, "note": "Ministral 3 14B; vision input"},
        "mistral/ministral-3b-latest": {**_PIN_ONLY, "note": "Ministral 3 3B; cheapest; vision input"},
    },
)
