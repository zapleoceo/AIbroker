"""Cost estimation — LiteLLM's pricing map plus our own overrides.

Models litellm does not know are listed in the provider registry with
pricing="override" and registered here at import, so every cost path (and every
daily $ cap) prices them like any other model. Per-minute audio prices come from
the registry too (ModelSpec.usd_per_minute).
"""
from __future__ import annotations

import logging
from datetime import datetime

import litellm

from aibroker.providers.peak_pricing import peak_multiplier
from aibroker.providers.pricing import MIN_BILLED_AUDIO_S, audio_surcharge
from aibroker.providers.prompt_cache import _CACHE_TTL, _CACHE_TTL_RATE_FIELD
from aibroker.providers.registry import all_models, model_spec

log = logging.getLogger(__name__)

# Gemini tokenises audio at ~32 tokens per second.
GEMINI_AUDIO_TOKENS_PER_S = 32


def register_price_overrides() -> None:
    """Push every registry "override" price — and every "free" model, at 0.0, so it
    counts as a KNOWN free model rather than an unpriced one — into litellm's map."""
    entries = {}
    for m in all_models():
        if m.pricing == "free" and m.register:
            entries[m.id] = {"input_cost_per_token": 0.0, "output_cost_per_token": 0.0,
                             "litellm_provider": m.provider, "mode": "chat", **dict(m.litellm_extra)}
            continue
        if m.pricing != "override" or not m.register:
            continue
        entry = {
            "input_cost_per_token": (m.input_usd_per_mtok or 0.0) / 1_000_000,
            "output_cost_per_token": (m.output_usd_per_mtok or 0.0) / 1_000_000,
            **dict(m.litellm_extra),
        }
        if m.cache_read_usd_per_mtok is not None:
            entry["cache_read_input_token_cost"] = m.cache_read_usd_per_mtok / 1_000_000
        entries[m.id] = entry
    if entries:
        litellm.register_model(entries)


register_price_overrides()


_pricing_warned: set[str] = set()


def estimate_llm_cost(
    model: str, tokens_in: int, tokens_out: int, *,
    at: datetime | None = None,
    cache_read_tokens: int = 0, cache_write_tokens: int = 0,
    audio_input_tokens: int = 0,
) -> float:
    """Real per-model cost from LiteLLM's pricing map, times any time-of-day
    surcharge (DeepSeek peak/valley). Returns 0.0 only when the model is
    genuinely unpriced — and logs that once per model so a silent pricing break
    can't hide (a `completion_cost` signature change zeroed every cost for days
    before we noticed, blinding the cost guard). `at` overrides the clock in
    tests; production passes None (= now, UTC).

    `cache_read_tokens`/`cache_write_tokens` are the SUBSET of `tokens_in` that
    hit/wrote anthropic's prompt cache (see apply_prompt_cache) — passed through
    to LiteLLM so a cache read prices at ~0.1x and a cache write at its real
    (higher) creation rate, instead of every prompt token pricing at the flat
    input rate. Without this, cost_usd over-counted anthropic calls that hit
    cache — safe direction (never under-charges) but not the real bill.

    Extended-TTL correction: `litellm.cost_per_token` prices EVERY cache write
    at the 5-minute rate — it has no ttl parameter at all (verified on our
    version). We write with a 1-hour TTL (see _CACHE_TTL), which anthropic bills
    at a higher rate that litellm exposes as a separate, unused pricing field.
    So the premium is added here explicitly; skipping it would UNDER-count every
    write by that difference and quietly blind the daily cost caps — the same
    failure mode as the two stale-pricing incidents (2026-06-01, 2026-06-11).
    Rates are read from litellm's map (never hardcoded), so they stay correct
    when the vendor's prices change."""
    try:
        p_cost, c_cost = litellm.cost_per_token(
            model=model, prompt_tokens=tokens_in, completion_tokens=tokens_out,
            cache_read_input_tokens=cache_read_tokens,
            cache_creation_input_tokens=cache_write_tokens,
        )
        base = float(p_cost + c_cost) + _extended_ttl_write_premium(
            model, cache_write_tokens) + audio_surcharge(model, audio_input_tokens)
    except Exception as e:
        if model not in _pricing_warned:
            _pricing_warned.add(model)
            log.warning("no LiteLLM pricing for %s (%s) — cost recorded as 0", model, e)
        return 0.0
    return base * peak_multiplier(model.split("/", 1)[0], at)


def _extended_ttl_write_premium(model: str, cache_write_tokens: int) -> float:
    """Extra cost of writing the cache at `_CACHE_TTL` instead of the default
    5 minutes, which is all `litellm.cost_per_token` can price. Returns 0.0 when
    we're not using an extended TTL, when nothing was written, or when the model
    has no separate long-TTL rate (then the default rate already applies)."""
    if _CACHE_TTL is None or cache_write_tokens <= 0:
        return 0.0
    try:
        info = litellm.get_model_info(model)
    except Exception:
        return 0.0
    default_rate = info.get("cache_creation_input_token_cost")
    extended_rate = info.get(_CACHE_TTL_RATE_FIELD)
    if not default_rate or not extended_rate:
        return 0.0
    return cache_write_tokens * (float(extended_rate) - float(default_rate))



# Per-minute audio prices: ModelSpec.usd_per_minute in the provider registry.
# Voice notes are opus/ogg at ~24-32 kbps; mp3 uploads run higher, which only
# makes this OVER-estimate duration (safe direction for a cost reservation).
_ASSUMED_AUDIO_BPS = 32_000


def _estimate_audio_seconds(n_bytes: int) -> float:
    return n_bytes * 8 / _ASSUMED_AUDIO_BPS


def whisper_cost(model: str, audio_s: float) -> float:
    """Per-minute price × duration for a whisper-style model; 0.0 if unpriced."""
    spec = model_spec(model)
    rate = spec.usd_per_minute if spec and spec.pricing == "per_minute" else None
    if rate is None:
        if model not in _pricing_warned:
            _pricing_warned.add(model)
            log.warning("no per-minute pricing for %s — transcription cost recorded as 0", model)
        return 0.0
    # Providers with a per-request floor (groq: 10 s) bill short clips at it.
    audio_s = max(audio_s, MIN_BILLED_AUDIO_S.get(model.split("/", 1)[0], 0.0))
    return rate * audio_s / 60.0


def estimate_transcription_cost(model: str, n_bytes: int) -> float:
    """Worst-case-leaning cost of transcribing `n_bytes` of audio with `model`,
    for the cost guard's reservation BEFORE the call (the real duration is
    only known afterwards). Whisper models price by minute; chat-based
    transcription (gemini) prices by token, ~32 audio tokens per second."""
    audio_s = _estimate_audio_seconds(n_bytes)
    spec = model_spec(model)
    if spec and spec.pricing == "per_minute":
        return whisper_cost(model, audio_s)
    if spec and spec.pricing == "local":
        return 0.0
    tokens_in = int(audio_s * GEMINI_AUDIO_TOKENS_PER_S)
    return estimate_llm_cost(model, tokens_in, 2048, audio_input_tokens=tokens_in)
