"""Definition for the mistral provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

SPEC = build_provider(
    "mistral", rank=80, cooldown_base_s=10,           # 1 RPS — recovers instantly
    cache_key_param="prompt_cache_key",   # docs: docs.mistral.ai/api/endpoint/chat
    rate_limit_signs=("unauthorized",), monthly_signs=("unauthorized",),
    quota_headers="openai",               # bare 401 = monthly plan exhaustion (not a revoked key)
    quota=Quota(doc="https://docs.mistral.ai/deployment/laplateforme/tier/"),
    probe=ProbeSpec("https://api.mistral.ai/v1/models"),
    billing_probe=chat_probe("https://api.mistral.ai/v1/chat/completions", "mistral-small-latest"),
    defaults={"chat:fast": "mistral/mistral-small-latest",
              "chat:smart": "mistral/mistral-large-latest",
              "chat:code": "mistral/codestral-latest",
              "prefilter": "mistral/mistral-small-latest",
              "structured": "mistral/mistral-small-latest",
              "translate": "mistral/mistral-small-latest"},
)
