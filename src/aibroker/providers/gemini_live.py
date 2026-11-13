"""Google Gemini Live (speech-to-speech) over its BidiGenerateContent WebSocket.

One `GeminiLiveSession` is one upstream conversation: the client's microphone goes
in as 16 kHz mono 16-bit PCM (`realtimeInput.audio`), the model answers with
24 kHz mono 16-bit PCM in `serverContent.modelTurn.parts[].inlineData`. Google's
own voice activity detection decides when the caller finished a phrase and when
they talk over the answer (`interrupted`). Both sides are transcribed
(`inputAudioTranscription` / `outputAudioTranscription`) so the caller can keep
a text record of the call.

Cost: Live bills per token by modality. Every `usageMetadata` reports the tokens
of one model turn (the whole context the turn read, so later turns re-bill the
history); the session total is their sum. Text and audio tokens have separate
list prices (ModelSpec input/output + audio_input/audio_output).

`websockets` arrives with `uvicorn[standard]` (the server already depends on it).
"""
from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from aibroker.providers.registry import model_spec

log = logging.getLogger(__name__)

GEMINI_LIVE_URL = ("wss://generativelanguage.googleapis.com/ws/"
                   "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")
INPUT_RATE_HZ = 16_000
OUTPUT_RATE_HZ = 24_000
INPUT_MIME = f"audio/pcm;rate={INPUT_RATE_HZ}"
# Reservation rates. Google's per-minute prices imply ~28 audio tokens per second
# in and 25 out (2026-11 pricing page); 32 in keeps the reservation on the high side.
AUDIO_IN_TOKENS_PER_S = 32
AUDIO_OUT_TOKENS_PER_S = 25


@dataclass
class LiveUsage:
    """Running token totals of one session, split by modality."""
    text_in: int = 0
    audio_in: int = 0
    text_out: int = 0
    audio_out: int = 0

    @property
    def tokens_in(self) -> int:
        return self.text_in + self.audio_in

    @property
    def tokens_out(self) -> int:
        return self.text_out + self.audio_out

    def add(self, meta: dict[str, Any]) -> None:
        """Fold one `usageMetadata` in. Details by modality when Google sends them;
        otherwise the totals count as audio (the dearer rate, never under-billed)."""
        prompt = int(meta.get("promptTokenCount") or 0)
        response = int(meta.get("responseTokenCount") or meta.get("candidatesTokenCount") or 0)
        audio_in = _modality(meta.get("promptTokensDetails"), "AUDIO")
        audio_out = _modality(meta.get("responseTokensDetails")
                              or meta.get("candidatesTokensDetails"), "AUDIO")
        audio_in = prompt if audio_in is None else audio_in
        audio_out = response if audio_out is None else audio_out
        self.audio_in += audio_in
        self.text_in += max(prompt - audio_in, 0)
        self.audio_out += audio_out
        self.text_out += max(response - audio_out, 0)


def _modality(details: Any, modality: str) -> int | None:
    if not isinstance(details, list):
        return None
    return sum(int(d.get("tokenCount") or 0) for d in details
               if isinstance(d, dict) and d.get("modality") == modality)


def live_cost(model: str, usage: LiveUsage) -> float:
    """Our list price for the session; 0.0 when the model is not priced."""
    spec = model_spec(model)
    if spec is None or spec.input_usd_per_mtok is None or spec.output_usd_per_mtok is None:
        log.warning("no per-token pricing for %s — live cost recorded as 0", model)
        return 0.0
    audio_in = spec.audio_input_usd_per_mtok or spec.input_usd_per_mtok
    audio_out = spec.audio_output_usd_per_mtok or spec.output_usd_per_mtok
    return (usage.text_in * spec.input_usd_per_mtok + usage.audio_in * audio_in
            + usage.text_out * spec.output_usd_per_mtok + usage.audio_out * audio_out) / 1e6


def estimate_live_cost(model: str, max_seconds: float) -> float:
    """Reservation for a whole session: the caller speaks and the model answers for
    the full allowed time (an over-estimate: in a real call they take turns)."""
    seconds = max(max_seconds, 1.0)
    return live_cost(model, LiveUsage(audio_in=int(seconds * AUDIO_IN_TOKENS_PER_S),
                                      audio_out=int(seconds * AUDIO_OUT_TOKENS_PER_S)))


def setup_message(*, model: str, voice: str, system: str, language: str | None) -> dict[str, Any]:
    """The first frame of a session."""
    speech: dict[str, Any] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
    if language:
        speech["languageCode"] = language
    setup: dict[str, Any] = {
        "model": f"models/{model.split('/', 1)[1]}",
        "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }
    if system.strip():
        setup["systemInstruction"] = {"parts": [{"text": system}]}
    return {"setup": setup}


def audio_message(pcm: bytes) -> dict[str, Any]:
    return {"realtimeInput": {"audio": {"data": base64.b64encode(pcm).decode(),
                                        "mimeType": INPUT_MIME}}}


def text_message(text: str) -> dict[str, Any]:
    """A typed user turn (used to make the model greet the caller first)."""
    return {"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}],
                              "turnComplete": True}}


@dataclass(frozen=True)
class LiveEvent:
    """One thing the upstream said. kind: audio | input_text | output_text |
    interrupted | turn_complete | go_away."""
    kind: str
    audio: bytes = b""
    text: str = ""


@dataclass
class ParsedMessage:
    events: list[LiveEvent] = field(default_factory=list)
    usage: dict[str, Any] | None = None


def parse_server_message(msg: dict[str, Any]) -> ParsedMessage:
    """Translate one upstream frame into events (unknown frames yield nothing)."""
    out = ParsedMessage(usage=msg.get("usageMetadata") if isinstance(
        msg.get("usageMetadata"), dict) else None)
    content = msg.get("serverContent") or {}
    for part in ((content.get("modelTurn") or {}).get("parts") or []):
        inline = part.get("inlineData") or {}
        if inline.get("data"):
            out.events.append(LiveEvent("audio", audio=base64.b64decode(inline["data"])))
    for key, kind in (("inputTranscription", "input_text"),
                      ("outputTranscription", "output_text")):
        text = (content.get(key) or {}).get("text")
        if text:
            out.events.append(LiveEvent(kind, text=text))
    if content.get("interrupted"):
        out.events.append(LiveEvent("interrupted"))
    if content.get("turnComplete"):
        out.events.append(LiveEvent("turn_complete"))
    if "goAway" in msg:
        out.events.append(LiveEvent("go_away"))
    return out


class WebSocketLike(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


class LiveClosed(Exception):
    """The upstream socket closed (normally or not); the session is over."""


class GeminiLiveSession:
    """A connected, set-up Live conversation. Not reusable after `close`."""

    def __init__(self, ws: WebSocketLike, model: str) -> None:
        self.ws = ws
        self.model = model
        self.usage = LiveUsage()
        self.model_served: str | None = None

    async def setup(self, *, voice: str, system: str, language: str | None) -> None:
        """Send `setup` and wait for `setupComplete`. Errors carry the reason so
        classify_provider_error can tell auth from quota."""
        await self.ws.send(json.dumps(setup_message(model=self.model, voice=voice,
                                                    system=system, language=language)))
        reply = _decode(await self.ws.recv())
        if "setupComplete" not in reply:
            raise RuntimeError(f"gemini-live setup refused: {json.dumps(reply)[:200]}")

    async def send_audio(self, pcm: bytes) -> None:
        await self.ws.send(json.dumps(audio_message(pcm)))

    async def send_text(self, text: str) -> None:
        await self.ws.send(json.dumps(text_message(text)))

    async def events(self) -> AsyncIterator[LiveEvent]:
        """Upstream events until the socket closes; usage is folded in as it comes."""
        while True:
            try:
                raw = await self.ws.recv()
            except LiveClosed:
                return
            except Exception as e:  # noqa: BLE001 — websockets.ConnectionClosed* and friends
                if type(e).__name__.startswith("ConnectionClosed"):
                    return
                raise
            parsed = parse_server_message(_decode(raw))
            if parsed.usage:
                self.usage.add(parsed.usage)
            for event in parsed.events:
                yield event

    async def close(self) -> None:
        try:
            await self.ws.close()
        except Exception:  # noqa: BLE001 — closing an already dead socket
            log.debug("gemini-live close failed", exc_info=True)


def _decode(raw: str | bytes) -> dict[str, Any]:
    data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    return data if isinstance(data, dict) else {}


async def connect_gemini_live(  # pragma: no cover — thin network I/O, the session is tested on a fake socket
    *, model: str, voice: str, system: str, language: str | None, api_key: str,
    timeout: float,
) -> GeminiLiveSession:
    """Open the socket (key in a header, never the URL) and run setup."""
    import asyncio

    import websockets

    try:
        ws = await asyncio.wait_for(websockets.connect(
            GEMINI_LIVE_URL, additional_headers={"x-goog-api-key": api_key},
            max_size=None, open_timeout=timeout), timeout)
    except websockets.InvalidStatus as e:
        status = e.response.status_code
        body = (e.response.body or b"").decode(errors="replace")[:200]
        raise RuntimeError(f"gemini-live {status}: {body}") from e
    session = GeminiLiveSession(ws, model)
    try:
        await asyncio.wait_for(session.setup(voice=voice, system=system, language=language),
                               timeout)
    except BaseException:
        await session.close()
        raise
    return session


class GeminiLiveTransport:
    """LiveTransport for the Gemini Live models."""

    async def open_live(self, *, model: str, voice: str, system: str, language: str | None,
                        api_key: str, timeout: float) -> GeminiLiveSession:
        return await connect_gemini_live(model=model, voice=voice, system=system,
                                         language=language, api_key=api_key, timeout=timeout)
