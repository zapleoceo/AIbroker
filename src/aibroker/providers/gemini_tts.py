"""Google Gemini text-to-speech via the raw generateContent endpoint.

The TTS models (gemini-3.8-flash-tts, gemini-3.8-flash-lite-tts) answer
generateContent with `responseModalities: ["AUDIO"]` and a prebuilt voice in
`speechConfig`; the reply part carries `inlineData` (audio/wav, 24 kHz mono
16-bit PCM, verified live 2026-11-11). litellm has no route for it, so it is a
raw transport like gemini_asr.

Cost is our own per-token list price (ModelSpec input/output_usd_per_mtok):
text tokens in, audio tokens out (Google: 25 audio tokens per second).
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from aibroker.config import get_settings
from aibroker.providers.litellm_client import _bounded
from aibroker.providers.model_identity import served_model
from aibroker.providers.registry import model_spec

log = logging.getLogger(__name__)

_GEMINI_TTS_URL = "https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent"
GEMINI_AUDIO_OUT_TOKENS_PER_S = 25
# Plain text is roughly 4 characters per token; used only for the pre-call reservation.
_CHARS_PER_TOKEN = 4
# Russian speech runs ~14 characters per second; the reservation over-estimates
# by assuming a slower 10 chars/s so it never under-reserves.
_RESERVE_CHARS_PER_AUDIO_S = 10


def tts_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Our list price for one call; 0.0 when the model is not priced."""
    spec = model_spec(model)
    if spec is None or spec.input_usd_per_mtok is None or spec.output_usd_per_mtok is None:
        log.warning("no per-token pricing for %s — speech cost recorded as 0", model)
        return 0.0
    return (tokens_in * spec.input_usd_per_mtok + tokens_out * spec.output_usd_per_mtok) / 1e6


def estimate_speech_cost(model: str, text: str) -> float:
    """Worst-case-leaning cost for the cost guard's reservation BEFORE the call."""
    audio_s = max(len(text) / _RESERVE_CHARS_PER_AUDIO_S, 1.0)
    tokens_out = int(audio_s * GEMINI_AUDIO_OUT_TOKENS_PER_S)
    tokens_in = len(text) // _CHARS_PER_TOKEN + 16
    return tts_cost(model, tokens_in, tokens_out)


def speech_prompt(text: str, style: str | None) -> str:
    """The TTS models read a natural-language delivery hint before the text."""
    return f"{style.strip()}: {text}" if style and style.strip() else text


async def _post_gemini_tts(url: str, api_key: str, body: dict[str, Any],
                           timeout: float) -> httpx.Response:  # pragma: no cover — thin network I/O, exercised via synthesize's mocked tests
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=body, headers={"x-goog-api-key": api_key})


async def _post_bounded_tts(url: str, api_key: str, body: dict[str, Any], timeout: float,
                            max_response_bytes: int) -> httpx.Response:
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, json=body, headers={
            "x-goog-api-key": api_key, "accept-encoding": "identity",
        }) as response:
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("speech response uses unexpected encoding")
            raw = bytearray()
            async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                if len(raw) + len(chunk) > max_response_bytes:
                    raise ValueError("speech response exceeds byte limit")
                raw.extend(chunk)
            return httpx.Response(response.status_code, content=bytes(raw))


async def synthesize_via_gemini(
    *, model: str, text: str, voice: str, style: str | None, api_key: str,
    max_response_bytes: int | None = None,
    retain_unusable_audio: bool = False,
) -> tuple[bytes, dict[str, Any]]:
    """Text → audio bytes. Errors carry the HTTP status + body text so
    classify_provider_error (429/RESOURCE_EXHAUSTED → rate_limit, 401/403 →
    auth) works; a reply without audio raises so the key walk moves on."""
    name = model.split("/", 1)[1]
    body = {
        "contents": [{"parts": [{"text": speech_prompt(text, style)}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}},
        },
    }
    timeout = get_settings().GEMINI_TTS_TIMEOUT_S
    t0 = time.time()
    try:
        url = _GEMINI_TTS_URL.format(name=name)
        if max_response_bytes is not None:
            post = _post_bounded_tts(url, api_key, body, timeout, max_response_bytes)
        else:
            post = _post_gemini_tts(url, api_key, body, timeout)
        resp = await _bounded(post, timeout, f"gemini-tts {name}")
    except httpx.TimeoutException as e:
        raise TimeoutError(f"gemini-tts timeout: {e}") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"gemini-tts unreachable: {e}") from e
    latency_ms = int((time.time() - t0) * 1000)
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini-tts {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    inline = next((p.get("inlineData") for p in parts if p.get("inlineData")), None) or {}
    if retain_unusable_audio:
        # Typed queue can book a known billed response even if the bytes are unusable.
        try:
            audio = base64.b64decode(inline.get("data") or "", validate=True)
        except (TypeError, ValueError):
            audio = b""
    else:
        audio = base64.b64decode(inline.get("data") or "")
        if not audio:
            raise RuntimeError("gemini-tts returned no audio")
    usage = data.get("usageMetadata") or {}
    if retain_unusable_audio:
        tokens_in = usage.get("promptTokenCount")
        tokens_out = usage.get("candidatesTokenCount")
        counts = (tokens_in, tokens_out)
        if (any(not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < 2**31 for n in counts)
                or tokens_out == 0):
            raise RuntimeError("gemini-tts result has no credible usage evidence")
    else:
        tokens_in = int(usage.get("promptTokenCount") or 0)
        tokens_out = int(usage.get("candidatesTokenCount") or 0)
    return audio, {
        "model": model,
        "model_served": served_model(model, data.get("modelVersion")),
        "mime_type": (inline.get("mimeType") if retain_unusable_audio
                      else inline.get("mimeType") or "audio/wav"),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": tts_cost(model, tokens_in, tokens_out),
        "audio_s": round(tokens_out / GEMINI_AUDIO_OUT_TOKENS_PER_S, 1),
        "latency_ms": latency_ms,
    }


class GeminiTtsTransport:
    """SpeechTransport for the Gemini TTS models."""

    async def synthesize(
        self, *, model: str, text: str, voice: str, style: str | None, api_key: str,
    ) -> tuple[bytes, dict[str, Any]]:
        return await synthesize_via_gemini(
            model=model, text=text, voice=voice, style=style, api_key=api_key)
