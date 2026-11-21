"""Definition for the openai provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

_CHAT_LANES = ("chat:fast", "chat:smart", "chat:code", "structured")


def _pin_only(note: str, schema: bool = False) -> dict:
    """A text model callers may pin; never walked unpinned. All gpt-6.x read images
    (models page: latest models take text+image input). Prices equal litellm's map and
    developers.openai.com/api/docs/pricing (standard tier, <=272k), 2026-11-21."""
    meta = {"capabilities": (*_CHAT_LANES, "vision"), "pricing": "litellm", "note": note}
    if schema:
        meta["litellm_extra"] = {"supports_response_schema": True}
    return meta


# Newest generation, pin-only (developers.openai.com/api/docs/models, 2026-11-21).
# json_schema documented in the structured-outputs guide for gpt-6.2-astra only; the
# guide is silent on the others, so they leave supports_response_schema unset.
_PINNED_TEXT_MODELS = {
    "openai/gpt-6.2-astra": _pin_only("GPT-6.2 Astra; $8/$40; cached $0.40", schema=True),
    "openai/gpt-6.2-sol": _pin_only("GPT-6.2 Sol; $2/$10; cached $0.10"),
    "openai/gpt-6.1-terra": _pin_only("GPT-6.1 Terra; $1/$5; cached $0.05"),
    "openai/gpt-6-luna": _pin_only("GPT-6 Luna; $0.10/$0.50; cached $0.01"),
}

SPEC = build_provider(
    "openai", rank=220, paid=True, cooldown_base_s=120, native_tools=True,
    cache_key_param="prompt_cache_key",   # docs: platform.openai.com/docs/guides/prompt-caching
    quota_headers="openai", quota=Quota(doc="https://platform.openai.com/docs/guides/rate-limits"),
    probe=chat_probe("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    defaults={"chat:fast": "openai/gpt-5-mini", "chat:smart": "openai/gpt-5",
              "chat:code": "openai/gpt-5", "structured": "openai/gpt-5-mini",
              "vision": "openai/gpt-5-mini", "transcription": "openai/whisper-1"},
    model_meta={"openai/whisper-1": {"pricing": "per_minute", "usd_per_minute": 0.006},
                **_PINNED_TEXT_MODELS},
)
