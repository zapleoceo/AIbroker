"""Pricing helpers shared by every cost path (chat, decisions, transcription).

Kept apart from litellm_adapter so decisions.py can use them without importing
the adapter, and so the per-model overrides live in one reviewable table.
See docs/pricing.md for the human-readable version of everything here.
"""
from __future__ import annotations

from typing import Any

import litellm

# USD per 1M AUDIO input tokens for models whose LiteLLM map entry has no
# `input_cost_per_audio_token` while Google bills audio above the text rate.
# Source: https://ai.google.dev/gemini-api/docs/pricing (checked 2026-10-03).
# Models LiteLLM already prices (gemini-3.5-flash, gemini-3-flash-preview...)
# are read from its map instead - this table is only the gap filler.
AUDIO_INPUT_USD_PER_M: dict[str, float] = {
    "gemini/gemini-2.5-flash": 1.00,
    "gemini/gemini-2.5-flash-lite": 0.30,
    "gemini/gemini-3.1-flash-lite": 0.50,
}

# Minimum billed audio seconds per request, by provider prefix. Groq bills
# every Whisper request as at least 10 s (https://console.groq.com/docs/speech-to-text).
MIN_BILLED_AUDIO_S: dict[str, float] = {"groq": 10.0}


def reported_cost(usage: Any) -> float | None:
    """The cost a provider put on the response itself (OpenRouter's
    `usage.cost`, in USD), or None when absent. The provider's own number beats
    any price table: it is the real bill, including BYOK/discount/route
    effects. Accepts a dict or an attribute object (LiteLLM keeps extra usage
    fields as attributes). A bool or non-numeric value is treated as absent."""
    raw = usage.get("cost") if isinstance(usage, dict) else getattr(usage, "cost", None)
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    return float(raw)


def audio_input_rate(model: str) -> float | None:
    """USD per audio input token for `model`, or None if no separate audio rate
    is known (callers then price audio like text)."""
    if model in AUDIO_INPUT_USD_PER_M:
        return AUDIO_INPUT_USD_PER_M[model] / 1e6
    try:
        rate = litellm.get_model_info(model).get("input_cost_per_audio_token")
    except Exception:  # noqa: BLE001 - unknown model: caller handles unpriced
        return None
    return float(rate) if rate else None


def audio_surcharge(model: str, audio_tokens: int) -> float:
    """Extra USD for `audio_tokens` billed at the audio rate instead of the text
    input rate (those tokens are already inside tokens_in, priced as text).
    Never negative - an audio rate below the text rate is ignored."""
    if audio_tokens <= 0:
        return 0.0
    audio = audio_input_rate(model)
    if audio is None:
        return 0.0
    try:
        text = float(litellm.get_model_info(model).get("input_cost_per_token") or 0.0)
    except Exception:  # noqa: BLE001
        return 0.0
    return max(audio - text, 0.0) * audio_tokens


def is_priced(model: str) -> bool:
    """True when LiteLLM's map (incl. our register_model entries) knows `model`.
    A registered price of 0.0 counts as priced - that is how free models are
    declared. Used by the pricing regression test."""
    try:
        info = litellm.get_model_info(model)
    except Exception:  # noqa: BLE001
        return False
    return info.get("input_cost_per_token") is not None or         info.get("input_cost_per_second") is not None
