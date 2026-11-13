"""Real-time speech-to-speech (`live` capability, WebSocket /v1/live).

A phone call between a caller (the client project relays its microphone) and a
Gemini Live voice. Same money rules as /v1/speech: PAID gemini keys carrying
`llm:live` only, the project and key caps apply. The whole session is ONE
key attempt (services/attempt.py): the reservation covers the longest allowed
session, the booked cost is what Google reports when the session ends.

Key rotation happens only while connecting: once the session is set up, the
caller's audio has gone to that key, so a later upstream failure ends the call
(booked with the usage so far) instead of silently moving to another key.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from aibroker.config import get_settings
from aibroker.db.models import ProjectRow
from aibroker.providers.catalog import resolve_pin
from aibroker.providers.gemini_live import (
    INPUT_RATE_HZ,
    OUTPUT_RATE_HZ,
    LiveEvent,
    estimate_live_cost,
    live_cost,
)
from aibroker.providers.registry import model_for
from aibroker.providers.transport import open_live
from aibroker.routing import chain_for, pick_and_reserve, scope_for
from aibroker.services.attempt import Attempt, Flow, run_attempt
from aibroker.services.llm_service import _failure_text, _max_keys, cap_message

log = logging.getLogger(__name__)

CAPABILITY = "live"


class LiveFailed(Exception):
    """Every paid key failed to open a session (route closes with 4502)."""


@dataclass(frozen=True)
class LiveRequest:
    voice: str
    system: str
    language: str | None = None
    model: str | None = None
    workflow: str | None = None
    greeting: str | None = None          # typed first user turn: the model speaks first
    max_seconds: float | None = None     # capped by LIVE_MAX_SESSION_S


class LiveClient(Protocol):
    """The caller side of the call (the route's WebSocket)."""

    async def receive(self) -> bytes | dict[str, Any] | None:
        """PCM bytes, a control message, or None once the caller hung up."""
    async def send_audio(self, pcm: bytes) -> None: ...
    async def send_event(self, event: dict[str, Any]) -> None: ...


@dataclass
class Transcript:
    """Who said what, consecutive fragments of one speaker merged."""
    turns: list[dict[str, str]] = field(default_factory=list)

    def add(self, role: str, text: str) -> None:
        if self.turns and self.turns[-1]["role"] == role:
            self.turns[-1]["text"] += text
        else:
            self.turns.append({"role": role, "text": text})

    def cleaned(self) -> list[dict[str, str]]:
        return [{"role": t["role"], "text": " ".join(t["text"].split())}
                for t in self.turns if t["text"].strip()]


@dataclass
class LiveOutcome:
    transcript: list[dict[str, str]]
    provider: str
    model: str
    voice: str
    duration_s: float
    tokens_in: int
    tokens_out: int
    cost_usd: float
    key_label: str
    request_id: int
    end_reason: str


_ROLE_OF = {"input_text": "user", "output_text": "assistant"}


async def _pump_upstream(session: Any, client: LiveClient, transcript: Transcript) -> str:
    async for event in session.events():
        await _forward(event, client, transcript)
        if event.kind == "go_away":
            return "upstream_go_away"
    return "upstream_closed"


async def _forward(event: LiveEvent, client: LiveClient, transcript: Transcript) -> None:
    if event.kind == "audio":
        await client.send_audio(event.audio)
    elif event.kind in _ROLE_OF:
        transcript.add(_ROLE_OF[event.kind], event.text)
        await client.send_event({"type": "transcript", "role": _ROLE_OF[event.kind],
                                 "text": event.text})
    elif event.kind in ("interrupted", "turn_complete"):
        await client.send_event({"type": event.kind})


async def _pump_client(session: Any, client: LiveClient) -> str:
    while True:
        message = await client.receive()
        if message is None:
            return "caller_hung_up"
        if isinstance(message, bytes):
            if message:
                await session.send_audio(message)
        elif message.get("type") == "end":
            return "caller_ended"
        elif message.get("type") == "text" and str(message.get("text") or "").strip():
            await session.send_text(str(message["text"]))


async def relay(session: Any, client: LiveClient, *, max_seconds: float) -> tuple[Transcript, str]:
    """Carry audio both ways until either side stops or the time is up. Never raises
    for an upstream / caller failure: the session already cost money and must be
    booked, so a failure is only an end reason."""
    transcript = Transcript()
    upstream = asyncio.ensure_future(_pump_upstream(session, client, transcript))
    caller = asyncio.ensure_future(_pump_client(session, client))
    reason = "time_limit"
    try:
        done, _ = await asyncio.wait({upstream, caller}, timeout=max_seconds,
                                     return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.exception() is not None:
                log.warning("live relay ended on %s", type(task.exception()).__name__)
                reason = "upstream_error" if task is upstream else "caller_error"
            else:
                reason = task.result()
            break
    finally:
        for task in (upstream, caller):
            task.cancel()
        for task in (upstream, caller):
            with contextlib.suppress(BaseException):
                await task
        await session.close()
    return transcript, reason


def _session_call(req: LiveRequest, model: str, client: LiveClient, max_seconds: float,
                  ) -> Callable[[str], Awaitable[tuple[Any, dict[str, Any]]]]:
    async def call(plain: str) -> tuple[Any, dict[str, Any]]:
        session = await open_live(model=model, voice=req.voice, system=req.system,
                                  language=req.language, api_key=plain,
                                  timeout=get_settings().LIVE_CONNECT_TIMEOUT_S)
        await client.send_event({"type": "ready", "model": model, "voice": req.voice,
                                 "input_rate_hz": INPUT_RATE_HZ,
                                 "output_rate_hz": OUTPUT_RATE_HZ,
                                 "max_seconds": max_seconds})
        if req.greeting and req.greeting.strip():
            await session.send_text(req.greeting)
        t0 = time.monotonic()
        transcript, reason = await relay(session, client, max_seconds=max_seconds)
        duration = time.monotonic() - t0
        usage = session.usage
        return (transcript, reason), {
            "model": model, "model_served": session.model_served,
            "tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out,
            "cost_usd": live_cost(model, usage),
            "latency_ms": int(duration * 1000), "duration_s": round(duration, 1),
        }
    return call


async def run_live(*, project: ProjectRow, req: LiveRequest,
                   client: LiveClient) -> LiveOutcome | None:
    """One call. None → no paid key carries llm:live (route: 4503); LiveFailed →
    no key could open a session (4502)."""
    provider = chain_for(CAPABILITY)[0]
    model = (resolve_pin(req.model, CAPABILITY, [provider])[0].model if req.model
             else model_for(provider, CAPABILITY))
    if not model:
        return None
    ceiling = get_settings().LIVE_MAX_SESSION_S
    max_seconds = min(req.max_seconds or ceiling, ceiling)
    any_key_seen = False
    last_error: str | None = None
    for _ in range(_max_keys(provider)):
        key = await pick_and_reserve(provider, scope=scope_for(CAPABILITY),
                                      require_tier="paid", project_id=project.id,
                                      models=[model])
        if key is None:
            break
        any_key_seen = True
        res = await run_attempt(Attempt(
            key=key, project=project, provider=provider, model=model,
            capability=CAPABILITY, workflow=req.workflow,
            estimated_cost=estimate_live_cost(model, max_seconds),
            call=_session_call(req, model, client, max_seconds),
            cap_flow=Flow.NEXT_KEY, pinned_model=req.model, note_affinity=False,
        ))
        if res.flow is Flow.SUCCESS:
            (transcript, reason), meta = res.payload, res.meta
            return LiveOutcome(
                transcript=transcript.cleaned(), provider=provider, model=model,
                voice=req.voice, duration_s=meta["duration_s"],
                tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
                cost_usd=meta["cost_usd"], key_label=key.label,
                request_id=res.usage_id or 0, end_reason=reason,
            )
        last_error = _failure_text(res)
        if res.flow is Flow.BUDGET_EXHAUSTED:
            raise LiveFailed(cap_message(getattr(res.error, "window", "daily")))
    if not any_key_seen:
        return None
    raise LiveFailed(last_error or "all keys failed")
