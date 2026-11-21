"""Definition for the groq provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import OSS, chat_probe
from aibroker.providers.registry import Quota, build_provider

# Pin-only text models (catalog refresh 2026-11-21, upstream = groq models list on our
# free key). Prices match https://groq.com/pricing and litellm's map. json_schema:
# https://console.groq.com/docs/structured-outputs lists gpt-oss-20b and qwen3.8-27b
# (strict and best-effort). qwen3.8-27b reads images upstream, but groq is not in the
# vision chain, so it is not marked "vision". gpt-oss-safeguard-20b is a guard model: skipped.
_GROQ_CHAT_LANES = ("chat:fast", "chat:smart", "chat:code", "structured", "prefilter", "translate")
_PINNED_TEXT_MODELS = {
    "groq/openai/gpt-oss-20b": {
        "capabilities": _GROQ_CHAT_LANES, "pricing": "litellm",
        "litellm_extra": {"supports_response_schema": True}, "note": "$0.075/$0.30 per 1M"},
    "groq/qwen/qwen3.8-27b": {
        "capabilities": _GROQ_CHAT_LANES, "pricing": "litellm",
        "litellm_extra": {"supports_response_schema": True},
        "note": "$0.80/$4.00 per 1M; reasoning; image input upstream"},
}

SPEC = build_provider(
    "groq", rank=20, cooldown_base_s=60, max_request_tokens=8_000,
    json_reliability="unreliable",        # grammar-JSON mode 400s server-side
    quota=Quota(req_per_day=14_400, tok_per_day=500_000,
                doc="https://console.groq.com/docs/rate-limits"),
    probe=chat_probe("https://api.groq.com/openai/v1/chat/completions", f"openai/{OSS}"),
    quota_headers="openai",
    defaults={"chat:fast": f"groq/openai/{OSS}", "chat:smart": f"groq/openai/{OSS}",
              "chat:code": f"groq/openai/{OSS}", "prefilter": f"groq/openai/{OSS}",
              "structured": f"groq/openai/{OSS}", "translate": f"groq/openai/{OSS}",
              "transcription": "groq/whisper-large-v3-turbo"},
    model_meta={
        "groq/whisper-large-v3-turbo": {"pricing": "per_minute", "usd_per_minute": 0.04 / 60},
        "groq/whisper-large-v3": {"pricing": "per_minute", "usd_per_minute": 0.111 / 60},
        **_PINNED_TEXT_MODELS,
    },
)
