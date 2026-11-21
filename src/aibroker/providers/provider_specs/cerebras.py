"""Definition for the cerebras provider."""
from __future__ import annotations

from aibroker.providers.adapters import _CerebrasAdapter
from aibroker.providers.provider_specs.common import OSS, chat_probe
from aibroker.providers.registry import Quota, build_provider

# Pin-only text model (catalog refresh 2026-11-21; upstream = public catalog, the broker
# has no active cerebras key). Price from https://www.cerebras.ai/pricing / public models
# catalog: $0.99 in / $1.49 out per 1M, structured_outputs true, vision true (cerebras is
# not in the vision chain, so not marked "vision").
_CEREBRAS_CHAT_LANES = ("chat:fast", "chat:smart", "chat:code", "structured")

SPEC = build_provider(
    "cerebras", rank=10, max_keys=3, cooldown_base_s=60,
    adapter=_CerebrasAdapter(), json_reliability="unreliable",
    cache_key_param="prompt_cache_key",   # docs: inference-docs.cerebras.ai/capabilities/prompt-caching
    # Free tier is enforced on TOKENS/day, not requests/day.
    quota=Quota(tok_per_day=1_000_000,
                doc="https://inference-docs.cerebras.ai/support/rate-limits"),
    probe=chat_probe("https://api.cerebras.ai/v1/chat/completions", OSS),
    quota_headers="openai", trust_req_header=False,
    defaults={"chat:fast": f"cerebras/{OSS}", "chat:smart": f"cerebras/{OSS}",
              "chat:code": f"cerebras/{OSS}", "structured": f"cerebras/{OSS}"},
    model_meta={
        "cerebras/qwen-3.8-27b": {
            "capabilities": _CEREBRAS_CHAT_LANES, "pricing": "override",
            "input_usd_per_mtok": 0.99, "output_usd_per_mtok": 1.49,
            "litellm_extra": {"supports_response_schema": True},
            "note": "Qwen 3.8 27B; reasoning; 64k context"},
    },
)
