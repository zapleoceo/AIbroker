"""Self-hosted faster-whisper (asr-local service on the same host).

Free, private, no external rate limit; raw HTTP behind the TranscribeTransport
Protocol. Errors are reclassified so classify_provider_error cools the key.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

from aibroker.config import get_settings
from aibroker.providers.model_identity import served_model


async def _post_local_asr(url: str, audio: bytes, timeout: float) -> httpx.Response:  # pragma: no cover — thin network I/O, exercised via _transcribe_via_local_asr's mocked tests
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, content=audio)


async def _transcribe_via_local_asr(*, audio: bytes) -> tuple[str, dict[str, Any]]:
    """Self-hosted faster-whisper (vera3's asr-local service, same host) —
    free, private, no external rate limit. `language=auto`: asr-local's own
    default is 'ru' (vera3's own voice notes), but broker callers (e.g.
    Stepan2's mostly-Bahasa leads) must not be force-decoded through that —
    auto-detect is the only sane default for a multi-tenant caller."""
    settings = get_settings()
    base = settings.ASR_LOCAL_URL
    if not base:
        raise RuntimeError("ASR_LOCAL_URL not configured")
    t0 = time.time()
    try:
        resp = await _post_local_asr(
            f"{base}/transcribe?language=auto", audio, settings.ASR_LOCAL_TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        # Connection refused / read timeout — the service is down or still
        # mid-transcribe on its single serialized worker. Reclassified as
        # TimeoutError (not left as a generic HTTPError) so
        # classify_provider_error cools the key: a plain 'error' gets NO
        # cooldown at all, so every subsequent request would re-hit a dead
        # endpoint with zero backoff until it recovers on its own.
        raise TimeoutError(f"asr-local unreachable: {e}") from e
    latency_ms = int((time.time() - t0) * 1000)
    if resp.status_code >= 500:
        raise TimeoutError(f"asr-local {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise RuntimeError(f"asr-local {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    meta = {
        "model": "local/whisper",
        "model_served": served_model("local/whisper", data.get("model")),
        "tokens_in": 0, "tokens_out": 0,
        "cost_usd": 0.0, "latency_ms": latency_ms,
    }
    return (data.get("text") or "").strip(), meta



class LocalAsrTransport:
    async def transcribe(
        self, *, model: str, audio: bytes, filename: str, api_key: str,
    ) -> tuple[str, dict[str, Any]]:
        return await _transcribe_via_local_asr(audio=audio)
