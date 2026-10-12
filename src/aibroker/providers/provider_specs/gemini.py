"""Definition for the gemini provider."""
from __future__ import annotations

from aibroker.providers.adapters import _GeminiAdapter
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

GEMINI_CHAT_ROTATION = ("gemini/gemini-3.5-flash-lite",
                        "gemini/gemini-3.6-flash",
                        "gemini/gemini-3.1-flash-lite",
                        "gemini/gemini-3.5-flash")
GEMINI_VISION_ROTATION = ("gemini/gemini-3.5-flash-lite",
                          "gemini/gemini-3.5-flash",
                          "gemini/gemini-3.1-flash-lite")
GEMINI_CHAT_LANES = ("chat:fast", "chat:smart", "chat:sales", "chat:code",
                     "chat:edit", "structured", "prefilter", "translate")

SPEC = build_provider(
    "gemini", rank=30, max_keys=3, cooldown_base_s=60, adapter=_GeminiAdapter(),
    native_tools=True,
    transport_factories={"gemini_asr": "aibroker.providers.gemini_asr:GeminiAsrTransport"},
    quota=Quota(req_per_day=1_500, doc="https://ai.google.dev/gemini-api/docs/rate-limits"),
    # Free-quota providers (gemini 20/day/model, cohere 1000/month, mistral monthly,
    # openrouter :free ~50/day) probe via free unmetered key-list endpoints, never a
    # generation; paid ones keep a 1-token generation (a list 200s for a billing-dead key).
    probe=ProbeSpec("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1",
                    auth="gemini_key"),
    billing_probe=ProbeSpec(
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-2.5-flash-lite:generateContent",
        {"contents": [{"parts": [{"text": "ping"}]}],
         "generationConfig": {"maxOutputTokens": 1}}, auth="gemini"),
    defaults={"chat:fast": "gemini/gemini-2.5-flash", "chat:smart": "gemini/gemini-2.5-flash",
              "chat:sales": "gemini/gemini-2.5-flash", "chat:code": "gemini/gemini-2.5-flash",
              "chat:edit": "gemini/gemini-2.5-flash", "structured": "gemini/gemini-2.5-flash",
              "vision": "gemini/gemini-2.5-flash",
              "prefilter": "gemini/gemini-2.5-flash-lite",
              "translate": "gemini/gemini-2.5-flash-lite",
              "transcription": "gemini/gemini-3.5-transcribe"},
    rotation={**dict.fromkeys(GEMINI_CHAT_LANES, GEMINI_CHAT_ROTATION),
              "vision": GEMINI_VISION_ROTATION},
    model_meta={   # dedicated ASR model needs raw generateContent (not litellm)
        "gemini/gemini-3.5-transcribe": {
            "transport": "gemini_asr", "pricing": "per_minute", "usd_per_minute": 0.005},
    },
)
