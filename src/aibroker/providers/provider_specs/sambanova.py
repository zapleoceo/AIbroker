"""Definition for the sambanova provider."""
from __future__ import annotations

from aibroker.providers.adapters import _SambanovaAdapter
from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

# Pin-only text models (catalog refresh 2026-11-21). The broker has no active sambanova
# key, so upstream truth is https://docs.sambanova.ai/cloud/docs/get-started/supported-models
# (all "Preview"); prices from https://cloud.sambanova.ai/plans/pricing, 2026-11-21.
# Docs do not state json_schema support, so supports_response_schema stays unset.
# DeepSeek-V3.2 is absent from that docs page but is KEPT: it is the owner-pinned
# chat:smart/sales/code model (tests/test_chains.py guard) and no key can confirm removal.
_SAMBANOVA_CHAT_LANES = ("chat:fast", "chat:smart", "chat:sales", "chat:code", "prefilter")

SPEC = build_provider(
    "sambanova", rank=40, cooldown_base_s=120,
    adapter=_SambanovaAdapter(), quota_headers="openai",
    quota=Quota(req_per_day=20,
                doc="https://docs.sambanova.ai/cloud/docs/get-started/rate-limits"),
    probe=chat_probe("https://api.sambanova.ai/v1/chat/completions", "gemma-4-31B-it"),
    defaults={"chat:fast": "sambanova/gemma-4-31B-it",
              "chat:smart": "sambanova/DeepSeek-V3.2",
              "chat:sales": "sambanova/DeepSeek-V3.2",
              "chat:code": "sambanova/DeepSeek-V3.2",
              "prefilter": "sambanova/gemma-4-31B-it",
              "vision": "sambanova/gemma-4-31B-it"},
    model_meta={
        "sambanova/GLM-5.3": {
            "capabilities": _SAMBANOVA_CHAT_LANES, "pricing": "override",
            "input_usd_per_mtok": 2.80, "output_usd_per_mtok": 8.80,
            "note": "GLM-5.3; text only; 1M context"},
        "sambanova/MiniMax-M3.1-Flash": {
            "capabilities": (*_SAMBANOVA_CHAT_LANES, "vision"), "pricing": "override",
            "input_usd_per_mtok": 0.30, "output_usd_per_mtok": 1.20,
            "note": "MiniMax M3.1 Flash; image+video input; 1M context"},
    },
)
