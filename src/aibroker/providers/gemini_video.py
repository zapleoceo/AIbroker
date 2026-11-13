"""Google Gemini Omni video clips via the raw Interactions endpoint.

Veo was removed from the Gemini API (last models shut down 2026-10-22); Google's
named replacement is `gemini-omni-1.2-flash` on `POST /v1beta/interactions` with
`response_format.type = "video"`. litellm has no route for it, so it is a raw
transport like gemini_tts.

A generation is slow (tens of seconds to minutes) and is waited for in three
bounded stages under ONE ceiling (GEMINI_VIDEO_TIMEOUT_S):
  1. the create call; an interaction that answers `in_progress` is polled by id;
  2. with `delivery: "uri"` the clip is a hosted file, polled until `ACTIVE`;
  3. the download of the MP4 bytes.

Written from Google's published docs (2026-11); NOT verified against a live key.
The parser therefore accepts every documented variant: inline base64 `data` or a
`uri`, under `steps[].content[]` or `outputs[]`.

Cost is our own per-token list price (ModelSpec input/output_usd_per_mtok). Google
bills video output by tokens per second of video, by resolution (pricing page).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from aibroker.config import get_settings
from aibroker.providers.model_identity import served_model
from aibroker.providers.registry import model_spec

log = logging.getLogger(__name__)

_API = "https://generativelanguage.googleapis.com/v1beta"
_INTERACTIONS_URL = f"{_API}/interactions"
# Output tokens Google bills per second of generated video (pricing page, 2026-11).
VIDEO_TOKENS_PER_S = {"360p": 1931, "720p": 5792}
VIDEO_ASPECT_RATIOS = ("9:16", "16:9")
VIDEO_MIN_S, VIDEO_MAX_S = 3, 15
# Pre-call reservation only: prompt characters per token, and what one reference
# image is assumed to cost on input.
_CHARS_PER_TOKEN = 4
_IMAGE_TOKENS = 1500
_WAITING = frozenset({"in_progress", "queued", "pending", "processing"})
_FILE_ID_RE = re.compile(r"/files/([^/:?]+)")


class VideoRejected(ValueError):
    """The request itself was refused (HTTP 400, a blocked prompt, an answer with
    no clip): another key or a retry would be refused the same way."""


@dataclass(frozen=True)
class VideoRequest:
    """One clip: what the caller asked for, already validated by the route."""
    prompt: str
    duration_s: int
    aspect_ratio: str
    resolution: str
    image: bytes | None = None
    image_mime: str | None = None


def video_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Our list price for one clip; 0.0 when the model is not priced."""
    spec = model_spec(model)
    if spec is None or spec.input_usd_per_mtok is None or spec.output_usd_per_mtok is None:
        log.warning("no per-token pricing for %s — video cost recorded as 0", model)
        return 0.0
    return (tokens_in * spec.input_usd_per_mtok + tokens_out * spec.output_usd_per_mtok) / 1e6


def video_output_tokens(resolution: str, duration_s: int) -> int:
    """Billed output tokens of a clip, from Google's tokens-per-second table."""
    return VIDEO_TOKENS_PER_S[resolution] * duration_s


def estimate_video_cost(model: str, request: VideoRequest) -> float:
    """What the clip will cost, for the cost guard's reservation BEFORE the call."""
    tokens_in = (len(request.prompt) // _CHARS_PER_TOKEN + 16
                 + (_IMAGE_TOKENS if request.image else 0))
    return video_cost(model, tokens_in,
                      video_output_tokens(request.resolution, request.duration_s))


def interaction_body(model: str, request: VideoRequest) -> dict[str, Any]:
    """The create-interaction JSON: a text prompt, optionally led by a reference image."""
    prompt: Any = request.prompt
    if request.image:
        prompt = [
            {"type": "image", "data": base64.b64encode(request.image).decode(),
             "mime_type": request.image_mime or "image/jpeg"},
            {"type": "text", "text": request.prompt},
        ]
    return {
        "model": model.split("/", 1)[1],
        "input": prompt,
        "response_format": {
            "type": "video", "aspect_ratio": request.aspect_ratio,
            "resolution": request.resolution, "duration": f"{request.duration_s}s",
            "delivery": "uri",
        },
    }


def _video_part(data: dict[str, Any]) -> dict[str, Any] | None:
    """The first video item of an interaction, wherever the API version puts it."""
    parts: list[Any] = list(data.get("outputs") or [])
    for step in data.get("steps") or []:
        parts.extend((step or {}).get("content") or [])
    return next((p for p in parts if isinstance(p, dict) and p.get("type") == "video"), None)


async def _send(method: str, url: str, api_key: str, body: dict[str, Any] | None,
                timeout: float) -> httpx.Response:  # pragma: no cover — thin network I/O, exercised via the mocked transport tests
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        return await client.request(method, url, json=body,
                                    headers={"x-goog-api-key": api_key})


class _Budget:
    """The one wall-clock ceiling shared by create, polling and download."""

    def __init__(self, total_s: float) -> None:
        self.total_s = total_s
        self._end = time.monotonic() + total_s

    def left(self, what: str) -> float:
        left = self._end - time.monotonic()
        if left <= 0:
            # Deliberately NOT a TimeoutError: a clip that renders slowly says nothing
            # about the key, and cooling it would also stop speech and calls on it.
            raise RuntimeError(f"gemini-video gave up on {what} after {self.total_s:g}s")
        return left


async def _call(method: str, url: str, api_key: str, body: dict[str, Any] | None,
                budget: _Budget, what: str) -> httpx.Response:
    """One HTTP exchange inside the budget. Errors carry the HTTP status + body so
    classify_provider_error reads the status ("HTTP 429" -> rate_limit, 401/403 -> auth); a 400 is the
    request's own fault and becomes VideoRejected."""
    timeout = budget.left(what)
    try:
        resp = await asyncio.wait_for(_send(method, url, api_key, body, timeout), timeout)
    except (TimeoutError, httpx.TimeoutException) as e:
        raise RuntimeError(f"gemini-video gave up on {what} after {budget.total_s:g}s") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"gemini-video unreachable ({what}): {e}") from e
    if resp.status_code == 400:
        raise VideoRejected(f"gemini-video HTTP 400: {resp.text[:300]}")
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini-video HTTP {resp.status_code} ({what}): {resp.text[:200]}")
    return resp


async def _await_interaction(data: dict[str, Any], api_key: str, budget: _Budget,
                             poll_s: float) -> dict[str, Any]:
    """Poll an interaction the API answered as still running until it is terminal."""
    while str(data.get("status") or "").lower() in _WAITING:
        interaction_id = data.get("id")
        if not interaction_id:
            raise RuntimeError("gemini-video: running interaction carries no id")
        await asyncio.sleep(min(poll_s, budget.left("the interaction")))
        data = (await _call("GET", f"{_INTERACTIONS_URL}/{interaction_id}", api_key, None,
                            budget, "the interaction")).json()
    status = str(data.get("status") or "completed").lower()
    if status != "completed":
        raise RuntimeError(f"gemini-video interaction ended {status}")
    return data


async def _await_file(uri: str, api_key: str, budget: _Budget, poll_s: float) -> None:
    """Wait until the hosted clip is ACTIVE (a uri with no file id is fetched as is)."""
    found = _FILE_ID_RE.search(uri)
    if not found:
        return
    while True:
        info = (await _call("GET", f"{_API}/files/{found.group(1)}", api_key, None,
                            budget, "the video file")).json()
        state = str((info.get("file") or info).get("state") or "ACTIVE").upper()
        if state == "ACTIVE":
            return
        if state == "FAILED":
            raise RuntimeError("gemini-video: the hosted file ended FAILED")
        await asyncio.sleep(min(poll_s, budget.left("the video file")))


async def generate_via_gemini(
    *, model: str, request: VideoRequest, api_key: str,
) -> tuple[bytes, dict[str, Any]]:
    """Prompt (+ optional reference image) -> MP4 bytes, waiting out the generation."""
    settings = get_settings()
    budget = _Budget(settings.GEMINI_VIDEO_TIMEOUT_S)
    poll_s = settings.GEMINI_VIDEO_POLL_S
    t0 = time.time()
    created = (await _call("POST", _INTERACTIONS_URL, api_key,
                           interaction_body(model, request), budget, "the create call")).json()
    data = await _await_interaction(created, api_key, budget, poll_s)
    part = _video_part(data)
    if part is None:
        raise VideoRejected("gemini-video returned no clip (prompt refused or filtered)")
    if part.get("data"):
        video = base64.b64decode(part["data"])
    elif part.get("uri"):
        await _await_file(part["uri"], api_key, budget, poll_s)
        video = (await _call("GET", part["uri"], api_key, None, budget, "the download")).content
    else:
        video = b""
    if not video:
        raise RuntimeError("gemini-video returned an empty clip")
    usage = data.get("usage") or {}
    tokens_in = int(usage.get("total_input_tokens") or 0)
    tokens_out = int(usage.get("total_output_tokens")
                     or video_output_tokens(request.resolution, request.duration_s))
    return video, {
        "model": model,
        "model_served": served_model(model, data.get("model")),
        "mime_type": part.get("mime_type") or "video/mp4",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": video_cost(model, tokens_in, tokens_out),
        "video_s": float(request.duration_s),
        "latency_ms": int((time.time() - t0) * 1000),
    }


class GeminiVideoTransport:
    """VideoTransport for the Gemini Omni video models."""

    async def generate_video(
        self, *, model: str, request: VideoRequest, api_key: str,
    ) -> tuple[bytes, dict[str, Any]]:
        return await generate_via_gemini(model=model, request=request, api_key=api_key)
