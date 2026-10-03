"""Google's dedicated Gemini ASR model via the raw generateContent endpoint.

It needs generationConfig.audioTranscriptionConfig and takes NO text prompt
(without that config it returns HTTP 200 with EMPTY output), so it cannot go
through litellm. Bake-off numbers: docs/history/provider-choices.md.
"""
from __future__ import annotations

import base64
import logging
import time
from typing import Any

import httpx

from aibroker.config import get_settings
from aibroker.providers.cost import GEMINI_AUDIO_TOKENS_PER_S, _estimate_audio_seconds, whisper_cost
from aibroker.providers.litellm_client import _audio_mime, _bounded, _transcribe_via_chat
from aibroker.providers.model_identity import served_model

log = logging.getLogger(__name__)

_GEMINI_ASR_MODEL = "gemini/gemini-3.5-transcribe"
_GEMINI_ASR_FALLBACK = "gemini/gemini-2.5-flash"
_GEMINI_ASR_URL = "https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent"


async def _post_gemini_asr(url: str, api_key: str, body: dict[str, Any], timeout: float) -> httpx.Response:  # pragma: no cover — thin network I/O, exercised via _transcribe_via_gemini_asr's mocked tests
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=body, headers={"x-goog-api-key": api_key})


async def _transcribe_via_gemini_asr(
    *, model: str, audio: bytes, filename: str, api_key: str,
) -> tuple[str, dict[str, Any]]:
    """Dedicated Gemini ASR model via the raw generateContent endpoint. Errors
    carry the HTTP status + body text so classify_provider_error (string
    based: "429"/RESOURCE_EXHAUSTED → rate_limit, 401/403 → auth) works; an
    empty transcript raises so the chain falls through rather than returning
    a silent empty success."""
    name = model.split("/", 1)[1]
    body = {
        "contents": [{"parts": [{"inline_data": {
            "mime_type": _audio_mime(filename),
            "data": base64.b64encode(audio).decode(),
        }}]}],
        "generationConfig": {"audioTranscriptionConfig": {
            "languageCodes": [], "mode": "VERBATIM",
        }},
    }
    t0 = time.time()
    timeout = get_settings().GEMINI_ASR_TIMEOUT_S
    try:
        # httpx's timeout is per-PHASE (a slow-drip response never trips it), so
        # the whole POST also gets a hard wait_for ceiling.
        resp = await _bounded(
            _post_gemini_asr(_GEMINI_ASR_URL.format(name=name), api_key, body, timeout),
            timeout, f"gemini-asr {name}",
        )
    except httpx.TimeoutException as e:
        raise TimeoutError(f"gemini-asr timeout: {e}") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"gemini-asr unreachable: {e}") from e
    latency_ms = int((time.time() - t0) * 1000)
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini-asr {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    parts = ((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    pieces = []
    for part in parts:
        t = (part.get("audioTranscription") or {}).get("text") or part.get("text")
        if t:
            pieces.append(t)
    text = " ".join(p.strip() for p in pieces if p.strip()).strip()
    if not text:
        raise RuntimeError("gemini-asr returned an empty transcript")
    tokens_in = int((data.get("usageMetadata") or {}).get("promptTokenCount") or 0)
    audio_s = (tokens_in / GEMINI_AUDIO_TOKENS_PER_S) if tokens_in else _estimate_audio_seconds(len(audio))
    meta = {
        "model": model,
        "model_served": served_model(model, data.get("modelVersion")),
        "tokens_in": tokens_in,
        "tokens_out": 0,
        "cost_usd": whisper_cost(model, audio_s),
        "audio_s": round(audio_s, 1),
        "latency_ms": latency_ms,
    }
    return text, meta



class GeminiAsrTransport:
    """TranscribeTransport for gemini-3.5-transcribe, with an in-transport
    fallback to gemini-2.5-flash via chat (run_transcribe walks ONE model per
    provider, so a rotation cannot carry it)."""

    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, api_key: str,
    ) -> tuple[str, dict[str, Any]]:
        try:
            return await _transcribe_via_gemini_asr(
                model=model, audio=audio, filename=filename, api_key=api_key,
            )
        except Exception as e:  # noqa: BLE001 — fall back unless the key itself is bad
            # A bad key fails the fallback identically -> surface it at once;
            # anything else (429 on the ASR model's own quota, 5xx, empty) tries
            # gemini-2.5-flash on the same key.
            from aibroker.providers.provider_errors import classify_provider_error
            if classify_provider_error(e, "gemini") == "auth":
                raise
            log.warning("gemini ASR %s failed (%s) — falling back to %s",
                        model, e, _GEMINI_ASR_FALLBACK)
            try:
                return await _transcribe_via_chat(
                    model=_GEMINI_ASR_FALLBACK, audio=audio,
                    filename=filename, api_key=api_key,
                )
            except Exception as fb:
                raise fb from e
