"""Definition for the voyage provider."""
from __future__ import annotations

from aibroker.providers.registry import ProbeSpec, Quota, build_provider

SPEC = build_provider(
    "voyage", rank=300, cooldown_base_s=60,
    rate_limit_signs=("reduced rate limits",),
    quota=Quota(doc="https://docs.voyageai.com/docs/pricing"),
    probe=ProbeSpec("https://api.voyageai.com/v1/embeddings",
                    {"model": "voyage-4", "input": "."}),
    defaults={"embedding": "voyage/voyage-4"},
    model_meta={"voyage/voyage-4": {          # absent from litellm's map: $0.06/M, 200M free/mo
        "pricing": "override", "input_usd_per_mtok": 0.06, "output_usd_per_mtok": 0.0,
        "litellm_extra": {"litellm_provider": "voyage", "mode": "embedding"}}},
)
