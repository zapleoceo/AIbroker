"""Definition for the cloudflare provider."""
from __future__ import annotations

from aibroker.providers.adapters import _CloudflareAdapter
from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

CLOUDFLARE_CHAT_LANES = ("chat:fast", "chat:smart", "chat:code", "prefilter")


def _pin(inp: float, out: float, cache: float | None, note: str, *, vision: bool = False,
         litellm: bool = False) -> dict:
    """Pin-only Workers AI text model (never walked unpinned). Prices: USD per 1M tokens
    from the model's catalog `price` property (Workers AI models/search, 2026-11-21;
    https://developers.cloudflare.com/workers-ai/platform/pricing/). JSON mode docs
    (features/json-mode, 2026-11-21) list none of these, so response_schema stays unset."""
    meta: dict = {"capabilities": (*CLOUDFLARE_CHAT_LANES, *(("vision",) if vision else ())),
                  "note": note}
    if litellm:
        return {**meta, "pricing": "litellm"}
    meta.update(pricing="override", input_usd_per_mtok=inp, output_usd_per_mtok=out)
    if cache is not None:
        meta["cache_read_usd_per_mtok"] = cache
    return meta


_PINNED_TEXT_MODELS = {
    "cloudflare/@cf/google/gemma-4-26b-a4b-it": _pin(
        0.10, 0.30, 0.05, "Gemma 4 26B-A4B; vision; free-plan neurons OK", vision=True, litellm=True),
    "cloudflare/@cf/openai/gpt-oss-20b": _pin(
        0.20, 0.30, None, "gpt-oss 20B; free-plan neurons OK", litellm=True),
    "cloudflare/@cf/thinkingmachines/inkling-2-small": _pin(
        0.30, 1.00, 0.10, "Inkling 2 Small; vision; 1M ctx; Workers Paid plan only",
        vision=True, litellm=True),
    "cloudflare/@cf/qwen/qwen3.8-27b": _pin(
        0.45, 3.20, 0.05, "Qwen3.8 27B; vision; 262k ctx; reasoning"),
    "cloudflare/@cf/zai-org/glm-5.3": _pin(
        1.40, 4.40, 0.26, "GLM-5.3; 1M ctx; reasoning mandatory; Workers Paid plan only"),
    "cloudflare/@cf/deepseek-ai/deepseek-v4.1-flash-1017": _pin(
        0.30, 1.20, 0.006, "DeepSeek V4.1 Flash; vision; 1M ctx; Workers Paid plan only"),
}

SPEC = build_provider(
    "cloudflare", rank=60, cooldown_base_s=120, adapter=_CloudflareAdapter(),
    rate_limit_signs=("daily free allocation", "neurons"),
    quota=Quota(doc="https://developers.cloudflare.com/workers-ai/platform/pricing/"),
    probe=chat_probe(
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions",
        "@cf/openai/gpt-oss-120b", needs_account_id=True),
    # llava-1.5-7b-hf is gone from the Workers AI catalog (2026-11-21); vision now gemma-4.
    model_meta=_PINNED_TEXT_MODELS,
    defaults={"vision": "cloudflare/@cf/google/gemma-4-26b-a4b-it",
              "chat:fast": "cloudflare/@cf/openai/gpt-oss-120b",
              "chat:smart": "cloudflare/@cf/openai/gpt-oss-120b",
              "chat:code": "cloudflare/@cf/openai/gpt-oss-120b",
              "prefilter": "cloudflare/@cf/openai/gpt-oss-120b"},
)
