"""WebSocket /v1/live — a real-time voice call through Gemini Live.

Protocol (one socket = one call; `X-Project-Key` header on the handshake):

  client → {"type": "start", "voice": "Kore", "system": "...", "language": "ru-RU",
            "greeting": "...", "model": null, "workflow": "call", "max_seconds": 300}
  broker → {"type": "ready", "model", "voice", "input_rate_hz": 16000,
            "output_rate_hz": 24000, "max_seconds"}
  client → binary frames: caller audio, 16 kHz mono 16-bit little-endian PCM
           {"type": "text", "text": "..."}  (optional typed turn)
           {"type": "end"}                  (hang up; closing the socket does too)
  broker → binary frames: model audio, 24 kHz mono 16-bit little-endian PCM
           {"type": "transcript", "role": "user" | "assistant", "text": "..."}
           {"type": "interrupted"}    (caller talked over: drop the audio you buffered)
           {"type": "turn_complete"}
  broker → {"type": "done", "transcript": [...], "duration_s", "tokens_in", "tokens_out",
            "cost_usd", "request_id", "end_reason"}, then close 1000.

Errors: {"type": "error", "code": ..., "detail": "..."} then close with 4000 + the HTTP
status the same failure has on the REST routes (4401, 4403, 4422, 4429, 4502, 4503).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError, field_validator

from aibroker.auth import project_by_key
from aibroker.config import get_settings
from aibroker.providers.catalog import UnknownModel, resolve_pin
from aibroker.routes.proxy import SPEECH_VOICES
from aibroker.routing import chain_for, scope_for
from aibroker.services.live_service import LiveFailed, LiveRequest, run_live
from aibroker.services.request_cap import RequestCapExhausted, admit_request

log = logging.getLogger(__name__)

router = APIRouter(tags=["live"])

LIVE_SYSTEM_MAX_CHARS = 60_000


class LiveStart(BaseModel):
    type: str = Field(pattern="^start$")
    voice: str = "Kore"
    system: str = Field("", max_length=LIVE_SYSTEM_MAX_CHARS)
    language: str | None = Field(None, max_length=16, description="BCP-47, e.g. ru-RU")
    greeting: str | None = Field(None, max_length=2000)
    model: str | None = None
    workflow: str | None = Field(None, max_length=120)
    max_seconds: float | None = Field(None, gt=0)

    @field_validator("voice")
    @classmethod
    def _known_voice(_cls, v: str) -> str:
        if v not in SPEECH_VOICES:
            raise ValueError(f"unknown voice {v!r}")
        return v


class _Refused(Exception):
    def __init__(self, code: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class SocketClient:
    """LiveClient over a Starlette WebSocket."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws

    async def receive(self) -> bytes | dict[str, Any] | None:
        try:
            message = await self.ws.receive()
        except (WebSocketDisconnect, RuntimeError):
            return None
        if message.get("type") == "websocket.disconnect":
            return None
        if message.get("bytes") is not None:
            return message["bytes"]
        try:
            data = json.loads(message.get("text") or "")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def send_audio(self, pcm: bytes) -> None:
        await self.ws.send_bytes(pcm)

    async def send_event(self, event: dict[str, Any]) -> None:
        await self.ws.send_text(json.dumps(event, ensure_ascii=False))


async def _read_start(client: SocketClient) -> LiveStart:
    try:
        first = await asyncio.wait_for(client.receive(), get_settings().LIVE_START_TIMEOUT_S)
    except TimeoutError as e:
        raise _Refused(408, "no start message") from e
    if not isinstance(first, dict):
        raise _Refused(422, "the first message must be a JSON start message")
    try:
        start = LiveStart.model_validate(first)
    except ValidationError as e:
        raise _Refused(422, e.errors(include_url=False, include_context=False)[0]["msg"]) from e
    if start.model:
        try:
            resolve_pin(start.model, "live", chain_for("live"))
        except UnknownModel as e:
            raise _Refused(400, str(e)) from e
    return start


@router.websocket("/live")
async def live_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    client = SocketClient(ws)
    try:
        key = ws.headers.get("x-project-key")
        project = await project_by_key(key) if key else None
        if project is None:
            raise _Refused(401, "X-Project-Key required" if not key else "Invalid project key")
        if scope_for("live") not in (project.allowed_scopes or []):
            raise _Refused(403, "project lacks scope: llm:live")
        start = await _read_start(client)
        try:
            await admit_request(project)
        except RequestCapExhausted as e:
            raise _Refused(429, str(e)) from e
        try:
            outcome = await run_live(project=project, client=client, req=LiveRequest(
                voice=start.voice, system=start.system, language=start.language,
                model=start.model, workflow=start.workflow, greeting=start.greeting,
                max_seconds=start.max_seconds))
        except LiveFailed as e:
            raise _Refused(502, f"live failed: {e}") from e
        if outcome is None:
            raise _Refused(503, "no paid gemini key carries llm:live")
    except _Refused as r:
        await _close(ws, {"type": "error", "code": r.code, "detail": r.detail}, 4000 + r.code)
        return
    await _close(ws, {
        "type": "done", "transcript": outcome.transcript, "model": outcome.model,
        "voice": outcome.voice, "duration_s": outcome.duration_s,
        "tokens_in": outcome.tokens_in, "tokens_out": outcome.tokens_out,
        "cost_usd": outcome.cost_usd, "key_label": outcome.key_label,
        "request_id": outcome.request_id, "end_reason": outcome.end_reason,
    }, 1000)


async def _close(ws: WebSocket, event: dict[str, Any], code: int) -> None:
    """Best effort: the caller may already be gone (a hang-up is a normal end)."""
    try:
        await ws.send_text(json.dumps(event, ensure_ascii=False))
        await ws.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        log.debug("live socket already closed")
