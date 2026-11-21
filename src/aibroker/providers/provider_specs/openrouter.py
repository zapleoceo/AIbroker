"""Definition for the openrouter provider."""
from __future__ import annotations

from aibroker.providers.provider_specs.common import chat_probe
from aibroker.providers.registry import ProbeSpec, Quota, build_provider

# Newest flagship text models of the major families, pin-only (catalog refresh 2026-11-21).
# Prices per 1M tokens straight from OpenRouter's public /api/v1/models pricing fields
# (checked 2026-11-21); all list `structured_outputs` in supported_parameters and read images.
# Lane chat:code is the only chat lane whose chain includes openrouter (no `structured`).
# Paid ones need the paid key; the three `:free` ones run on the free key at $0.
_PIN_LANES = ("chat:code",)


def _paid(inp: float, out: float) -> dict:
    return {"pricing": "override", "capabilities": _PIN_LANES,
            "input_usd_per_mtok": inp, "output_usd_per_mtok": out,
            "litellm_extra": {"supports_response_schema": True}}


def _free() -> dict:
    return {"pricing": "free", "capabilities": _PIN_LANES,
            "litellm_extra": {"supports_response_schema": True}}


_PINNED_TEXT_MODELS = {
    "openrouter/anthropic/claude-fable-5.2": _paid(8.0, 40.0),
    "openrouter/anthropic/claude-sonnet-5.5": _paid(2.0, 10.0),
    "openrouter/anthropic/claude-haiku-5.5": _paid(0.1, 0.5),    # 0.5/2.5 above 100k prompt
    "openrouter/openai/gpt-6.2-sol": _paid(2.0, 10.0),           # 4/15 above 272k prompt
    "openrouter/google/gemini-4-hydrogen": _paid(1.5, 7.5),
    "openrouter/qwen/qwen4-max": _paid(1.425, 4.275),
    "openrouter/deepseek/deepseek-v4.1-flash-1017": _paid(0.15, 0.6),
    "openrouter/moonshotai/kimi-k3.1-pro": _paid(0.253, 8.8),
    "openrouter/z-ai/glm-5.3": _paid(0.0699, 1.4),
    "openrouter/mistralai/mistral-large-4-0": _paid(0.7, 2.1),
    "openrouter/inclusionai/ling-3.2-flash:free": _free(),
    "openrouter/particle-ai/particle-1:free": _free(),
    "openrouter/nvidia/nemotron-3.5-super-vl:free": _free(),
}

SPEC = build_provider(
    "openrouter", rank=70, cooldown_base_s=300, json_reliability="unreliable",
    transport_factories={"openrouter_decisions": "aibroker.providers.decisions:OpenRouterDecisions"},
    cache_key_param="session_id",         # docs: openrouter.ai/docs/guides/best-practices/prompt-caching
    quota_headers="openai",
    quota=Quota(req_per_day=200, doc="https://openrouter.ai/docs/api-reference/limits"),
    probe=ProbeSpec("https://openrouter.ai/api/v1/auth/key"),
    billing_probe=chat_probe("https://openrouter.ai/api/v1/chat/completions",
                              "openai/gpt-4o-mini"),
    defaults={"chat:fast": "openrouter/google/gemma-4-31b-it:free",
              "chat:smart": "openrouter/google/gemma-4-31b-it:free",
              "chat:code": "openrouter/google/gemma-4-31b-it:free",
              "prefilter": "openrouter/google/gemma-4-31b-it:free",
              "structured": "openrouter/google/gemma-4-31b-it:free",
              "vision": "openrouter/google/gemma-4-31b-it:free",
              "decision": "openrouter/typesafe/jev-1.13"},
    model_meta={   # decisions endpoint is not a litellm route; priced by our own list price
        # Price confirmed 2026-10-10 by OpenRouter's official endpoint
        # https://openrouter.ai/api/v1/models/typesafe/jev-1.13/endpoints (provider TypeSafe:
        # prompt 0.000000042, completion 0). The model is absent from the /models list but
        # live; registered in LiteLLM's map so pricing checks see the confirmed price.
        "openrouter/typesafe/jev-1.13": {
            "transport": "openrouter_decisions", "pricing": "override",
            "input_usd_per_mtok": 0.042, "output_usd_per_mtok": 0.0},
        # OpenRouter public catalog: prompt/completion 0 (checked 2026-10-11).
        "openrouter/google/gemma-4-31b-it:free": {"pricing": "free"},
        # Confirmed 2026-10-10: .../models/inception/mercury-decide:free/endpoints → prompt 0, completion 0.
        # Owner's /ask2many shortlist (2026-10-15). List prices per Mtok from OpenRouter's public
        # /api/v1/models (checked 2026-10-15). Paid: a project with a 0 money cap is refused.
        # Lane chat:code: the only chat lane whose chain includes openrouter.
        "openrouter/meta/muse-wonder": {"pricing": "override", "capabilities": ("chat:code",),
                                        "input_usd_per_mtok": 4.0, "output_usd_per_mtok": 20.0},
        "openrouter/x-ai/grok-4.7": {"pricing": "override", "capabilities": ("chat:code",),
                                     "input_usd_per_mtok": 2.0, "output_usd_per_mtok": 6.0},
        "openrouter/xiaomi/mimo-v2.6-pro": {"pricing": "override", "capabilities": ("chat:code",),
                                            "input_usd_per_mtok": 0.435, "output_usd_per_mtok": 0.87},
        "openrouter/qwen/qwen3.8-max-0902": {"pricing": "override", "capabilities": ("chat:code",),
                                             "input_usd_per_mtok": 2.0, "output_usd_per_mtok": 6.0},
        "openrouter/moonshotai/kimi-k3": {"pricing": "override", "capabilities": ("chat:code",),
                                          "input_usd_per_mtok": 0.192, "output_usd_per_mtok": 16.0},
        "openrouter/inception/mercury-decide:free": {
            "transport": "openrouter_decisions", "pricing": "free",
            "capabilities": ("decision",)},
        **_PINNED_TEXT_MODELS,
    },
)
