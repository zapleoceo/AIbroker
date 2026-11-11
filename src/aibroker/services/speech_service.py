"""Text-to-speech (`speech` capability, POST /v1/speech).

Gemini TTS on PAID keys only: the spend lands on the account whose billing the
owner tops up (a free-tier key would hand Google the text under free-tier
data terms and spread billed-lane traffic over keys with no spend limit).
A key must also carry the `llm:speech` scope, so the lane is opt-in per key
as well as per project.
"""
from __future__ import annotations

from dataclasses import dataclass

from aibroker.db.models import ProjectRow
from aibroker.providers.catalog import resolve_pin
from aibroker.providers.gemini_tts import estimate_speech_cost
from aibroker.providers.registry import model_for
from aibroker.providers.transport import synthesize
from aibroker.routing import chain_for, pick_and_reserve, scope_for
from aibroker.services.attempt import Attempt, Flow, run_attempt
from aibroker.services.llm_service import _failure_text, _max_keys, cap_message

CAPABILITY = "speech"


@dataclass
class SpeechOutcome:
    audio: bytes
    mime_type: str
    provider: str
    model: str
    voice: str
    audio_s: float
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int
    model_served: str | None = None


class SpeechFailed(Exception):
    """Every paid key failed (route maps to 502)."""


def _synthesize_call(model: str, text: str, voice: str, style: str | None):
    async def call(plain: str):
        return await synthesize(model=model, text=text, voice=voice, style=style,
                                api_key=plain)
    return call


async def run_speech(
    *,
    project: ProjectRow,
    text: str,
    voice: str,
    style: str | None,
    model: str | None,
    workflow: str | None,
) -> SpeechOutcome | None:
    """Text → audio, rotating the provider's PAID keys that carry llm:speech.

    None → no such key (503); SpeechFailed → every key tried and failed (502).
    A pinned `model` must serve `speech` (validated by the route)."""
    provider = chain_for(CAPABILITY)[0]
    use_model = (resolve_pin(model, CAPABILITY, [provider])[0].model if model
                 else model_for(provider, CAPABILITY))
    if not use_model:
        return None
    any_key_seen = False
    last_error: str | None = None
    for _ in range(_max_keys(provider)):
        key = await pick_and_reserve(provider, scope=scope_for(CAPABILITY),
                                      require_tier="paid", project_id=project.id,
                                      models=[use_model])
        if key is None:
            break
        any_key_seen = True
        res = await run_attempt(Attempt(
            key=key, project=project, provider=provider, model=use_model,
            capability=CAPABILITY, workflow=workflow,
            estimated_cost=estimate_speech_cost(use_model, text),
            call=_synthesize_call(use_model, text, voice, style),
            cap_flow=Flow.NEXT_KEY, pinned_model=model,
        ))
        if res.flow is Flow.SUCCESS:
            meta = res.meta
            return SpeechOutcome(
                audio=res.payload, mime_type=meta.get("mime_type") or "audio/wav",
                provider=provider, model=use_model, voice=voice,
                audio_s=float(meta.get("audio_s") or 0.0),
                tokens_in=meta.get("tokens_in", 0), tokens_out=meta.get("tokens_out", 0),
                cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
                key_label=key.label, request_id=res.usage_id or 0,
                model_served=meta.get("model_served"),
            )
        last_error = _failure_text(res)
        if res.flow is Flow.BUDGET_EXHAUSTED:
            raise SpeechFailed(cap_message(getattr(res.error, "window", "daily")))
    if not any_key_seen:
        return None
    raise SpeechFailed(last_error or "all keys failed")
