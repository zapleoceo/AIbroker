"""Local faster-whisper ASR — backs AIbroker's 'local' transcription provider.

One model instance (small, int8, CPU) loads at startup and lives for the
process lifetime. Requests are serialized with a lock: the host has 2
cores shared with production Stepan2/Vera, so parallel decodes would only
thrash the CPU without finishing faster.

2026-07-18: tried bumping to large-v3-turbo, then medium — both OOM-killed
loading directly on this host (swap already 100% full, no headroom for the
transient peak during download+int8 quantization). Stayed on `small`;
`beam_size=5` (below, up from greedy) is the accuracy lever that doesn't cost
extra RAM instead. See docs/deploy-ops.md "Local ASR" for the numbers.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import tempfile
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request

log = logging.getLogger("asr-local")

MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
CPU_THREADS = int(os.environ.get("WHISPER_CPU_THREADS", "1"))
# "auto": this service is multi-tenant (any broker project's voice traffic,
# e.g. Stepan2's mostly-Bahasa leads) — AIbroker's own caller always passes
# ?language=auto explicitly anyway; this is only the fallback for a direct
# caller that omits the query param.
DEFAULT_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "auto")
_MAX_AUDIO_BYTES = 25 * 1024 * 1024

_model: Any | None = None
_transcribe_lock = asyncio.Lock()


def get_model() -> Any:
    global _model
    if _model is None:  # pragma: no cover — real model load, exercised in prod
        from faster_whisper import WhisperModel

        log.info("loading whisper '%s' int8 cpu_threads=%s", MODEL_SIZE, CPU_THREADS)
        _model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8",
                              cpu_threads=CPU_THREADS)
        log.info("model ready")
    return _model


@asynccontextmanager
async def lifespan(_app: FastAPI):  # pragma: no cover — startup preload only
    await asyncio.to_thread(get_model)
    yield


app = FastAPI(title="asr-local", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "service": "asr-local", "model": MODEL_SIZE,
            "model_loaded": _model is not None}


def _decode_body(raw: bytes, content_type: str) -> bytes:
    if content_type.startswith("application/json"):
        try:
            return base64.b64decode(json.loads(raw)["b64"])
        except (KeyError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"bad json body: {e}") from e
    return raw


def _run_transcribe(audio: bytes, language: str | None) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(suffix=".audio") as f:
        f.write(audio)
        f.flush()
        # beam_size=5 (up from 1/greedy, 2026-07-18): low volume means the
        # slower search is affordable, and it noticeably helps accuracy on
        # non-English audio — the case the correction pass can't always save.
        segments, info = get_model().transcribe(
            f.name, language=language, beam_size=5, vad_filter=True)
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            # The VAD gate can clip a quiet / short / breathy REAL message to
            # nothing. Before returning empty (which the broker then escalates
            # to a paid cloud model), retry once WITHOUT the gate — recovers
            # faint speech locally and for free. Only pays the extra decode on
            # the rare empty-first-pass case.
            segments, info = get_model().transcribe(
                f.name, language=language, beam_size=5, vad_filter=False)
            text = " ".join(s.text.strip() for s in segments).strip()
    return {"text": text, "duration_s": round(info.duration, 1),
            "language": info.language}


@app.post("/transcribe")
async def transcribe(request: Request) -> dict[str, Any]:
    audio = _decode_body(await request.body(),
                         request.headers.get("content-type", ""))
    if not audio:
        raise HTTPException(status_code=400, detail="empty audio body")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"audio > {_MAX_AUDIO_BYTES} bytes")
    lang = request.query_params.get("language") or DEFAULT_LANGUAGE
    async with _transcribe_lock:
        return await asyncio.to_thread(
            _run_transcribe, audio, None if lang == "auto" else lang)
