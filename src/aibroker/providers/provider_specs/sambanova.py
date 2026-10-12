"""Definition for the sambanova provider."""
from __future__ import annotations

from aibroker.providers.adapters import _SambanovaAdapter
from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

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
)
