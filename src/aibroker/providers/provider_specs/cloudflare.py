"""Definition for the cloudflare provider."""
from __future__ import annotations

from aibroker.providers.adapters import _CloudflareAdapter
from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

SPEC = build_provider(
    "cloudflare", rank=60, cooldown_base_s=120, adapter=_CloudflareAdapter(),
    rate_limit_signs=("daily free allocation", "neurons"),
    quota=Quota(doc="https://developers.cloudflare.com/workers-ai/platform/pricing/"),
    probe=chat_probe(
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
        "@cf/openai/gpt-oss-120b", needs_account_id=True),
    model_meta={"cloudflare/@cf/llava-hf/llava-1.5-7b-hf": {
        "pricing": "free", "note": "no litellm price; free neuron budget, no card on file"}},
    defaults={"vision": "cloudflare/@cf/llava-hf/llava-1.5-7b-hf",
              "chat:fast": "cloudflare/@cf/openai/gpt-oss-120b",
              "chat:smart": "cloudflare/@cf/openai/gpt-oss-120b",
              "chat:code": "cloudflare/@cf/openai/gpt-oss-120b",
              "prefilter": "cloudflare/@cf/openai/gpt-oss-120b"},
)
