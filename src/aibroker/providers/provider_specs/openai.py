"""Definition for the openai provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import Quota, build_provider

SPEC = build_provider(
    "openai", rank=220, paid=True, cooldown_base_s=120, native_tools=True,
    cache_key_param="prompt_cache_key",   # docs: platform.openai.com/docs/guides/prompt-caching
    quota_headers="openai", quota=Quota(doc="https://platform.openai.com/docs/guides/rate-limits"),
    probe=chat_probe("https://api.openai.com/v1/chat/completions", "gpt-4o-mini"),
    defaults={"chat:fast": "openai/gpt-5-mini", "chat:smart": "openai/gpt-5",
              "chat:code": "openai/gpt-5", "structured": "openai/gpt-5-mini",
              "vision": "openai/gpt-5-mini", "transcription": "openai/whisper-1"},
    model_meta={"openai/whisper-1": {"pricing": "per_minute", "usd_per_minute": 0.006}},
)
