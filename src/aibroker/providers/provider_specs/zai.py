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
    probe=chat_probe("https://api.z.ai/api/paas/v4/chat/completions", "glm-5.3-flash"),
    # glm-4.7-flash / glm-4.5-flash left the free key's /models list (2026-11-21);
    # glm-5.3-flash is the current *-flash. Docs list it at $0.15 in / $0.50 out per
    # 1M (https://docs.z.ai/guides/overview/pricing, 2026-11-21) but our key is the
    # free tier, so it is booked as free like the other free-tier models.
    defaults={"chat:fast": "zai/glm-5.3-flash", "prefilter": "zai/glm-5.3-flash"},
    model_meta={"zai/glm-5.3-flash": {
        "pricing": "free", "note": "free-tier key; list price $0.15/$0.50 per 1M"}},
)
