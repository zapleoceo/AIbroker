"""Definition for the zai provider."""
from __future__ import annotations

from aibroker.providers.adapters import _ZaiAdapter
from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

SPEC = build_provider(
    "zai", rank=50, cooldown_base_s=60, adapter=_ZaiAdapter(),
    json_reliability="incapable",         # no response_format support at all
    auth_signs=("invalid api parameter",),
    quota=Quota(doc="https://docs.z.ai/guides/overview/quick-start"),
    probe=chat_probe("https://api.z.ai/api/paas/v4/chat/completions", "glm-4.5-flash"),
    defaults={"chat:fast": "zai/glm-4.7-flash", "prefilter": "zai/glm-4.7-flash"},
)
