"""HeyGen Video 1: one durable submit, then bounded polling and MP4 download."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from aibroker.providers.gemini_video import VideoRejected, VideoRequest
from aibroker.providers.provider_specs.heygen import HEYGEN_MODEL

_API = "https://api.heygen.com/v3/models/videos"
_WAITING = frozenset({"pending", "processing"})
_ID = re.compile(r"[A-Za-z0-9_-]{1,255}")
_CDN_SUFFIXES = (".heygen.ai", ".heygen.com")
_MAX_VIDEO_BYTES = 32 * 1024 * 1024
_TIMEOUT_S = 300
_POLL_S = 4
# Standard rate after launch promo; image reference mode is priced at 2x.
# https://help.heygen.com/en/articles/17272995
_RATE = {"480p": 0.02, "768p": 0.03}


def estimate_heygen_cost(request: VideoRequest) -> float:
    return _RATE[request.resolution] * request.duration_s * (2 if request.image else 1)


def _body(request: VideoRequest) -> dict[str, Any]:
    if request.resolution not in _RATE or not 5 <= request.duration_s <= 15:
        raise VideoRejected("HeyGen requires 5–15 seconds at 480p or 768p")
    data: dict[str, Any] = {
        "model": "heygen-video-1", "mode": "text_to_video", "prompt": request.prompt,
        "duration": request.duration_s, "resolution": request.resolution,
        "aspect_ratio": request.aspect_ratio, "prompt_enhancement": "disabled",
    }
    if request.image:
        data["mode"] = "reference_to_video"
        data["prompt"] = "The person in <Picture 1> is the subject. " + request.prompt
        data["reference_images"] = [{
            "type": "base64", "data": base64.b64encode(request.image).decode("ascii"),
            "media_type": request.image_mime or "image/jpeg",
        }]
    return data


async def _checked(
    client: httpx.AsyncClient, method: str, url: str, *, api_key: str,
    body: dict[str, Any] | None = None, idem: str | None = None,
) -> dict[str, Any]:
    headers = {"x-api-key": api_key}
    if idem:
        headers["Idempotency-Key"] = idem
    try:
        response = await client.request(method, url, json=body, headers=headers)
    except httpx.HTTPError:
        raise RuntimeError("HeyGen video API unreachable") from None
    if response.status_code in (400, 402, 404):
        raise VideoRejected(f"HeyGen video HTTP {response.status_code}; request unavailable or refused")
    if response.status_code not in (200, 202):
        raise RuntimeError(f"HeyGen video HTTP {response.status_code}")
    try:
        data = response.json()["data"]
    except (ValueError, TypeError, KeyError):
        raise RuntimeError("HeyGen video response did not contain a data envelope") from None
    if not isinstance(data, dict):
        raise RuntimeError("HeyGen video response data is not an object")
    return data


async def _download(client: httpx.AsyncClient, url: Any) -> bytes:
    if not isinstance(url, str):
        raise RuntimeError("HeyGen video returned no download URL")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        valid = (parsed.scheme == "https" and host.endswith(_CDN_SUFFIXES)
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("HeyGen video download host rejected")
    video = bytearray()
    try:
        # The signed CDN URL supplies its own auth: no provider key or redirects.
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise RuntimeError("HeyGen CDN video unavailable")
            async for chunk in response.aiter_bytes(chunk_size=65536):
                video.extend(chunk)
                if len(video) > _MAX_VIDEO_BYTES:
                    raise VideoRejected("HeyGen returned an oversized clip")
    except httpx.HTTPError:
        raise RuntimeError("HeyGen CDN video unavailable") from None
    if len(video) < 12 or video[4:8] != b"ftyp":
        raise VideoRejected("HeyGen did not return an MP4")
    return bytes(video)


class HeyGenVideoTransport:
    """No fallback or automatic POST replay after a durable submit attempt."""

    async def generate_video(
        self, *, model: str, request: VideoRequest, api_key: str, dispatch: Any = None,
    ) -> tuple[bytes, dict[str, Any]]:
        from aibroker.services.media_dispatch import MediaUnknown

        if dispatch is None or model != HEYGEN_MODEL:
            raise MediaUnknown("HeyGen requires its own durable paid-video dispatch")
        started = time.monotonic()
        body = _body(request)
        try:
            async with asyncio.timeout(_TIMEOUT_S), httpx.AsyncClient(
                timeout=25, follow_redirects=False,
            ) as client:
                state = await dispatch.load()
                if state["phase"] == "prepared":
                    # Workspace-safe key; the durable fence independently prevents resubmission.
                    idem = hashlib.sha256(
                        f"broker-heygen:{dispatch.job_id}:{state['fingerprint']}".encode()
                    ).hexdigest()
                    await dispatch.begin()
                    data = await _checked(client, "POST", _API, api_key=api_key,
                                          body=body, idem=idem)
                    job = data.get("video_id")
                    if not isinstance(job, str) or not _ID.fullmatch(job):
                        raise MediaUnknown("HeyGen receipt missing or malformed; no resubmit")
                    await dispatch.receipt(job)
                else:
                    job = state["provider_id"]
                    if state["phase"] not in {"submitted", "settled"} or not (
                        isinstance(job, str) and _ID.fullmatch(job)
                    ):
                        raise MediaUnknown("HeyGen prior submission unknown; no resubmit")
                    data = {"status": "pending"}
                while data.get("status") in _WAITING:
                    await asyncio.sleep(_POLL_S)
                    await dispatch.authorize_http()
                    data = await _checked(client, "GET", f"{_API}/{job}", api_key=api_key)
                    if data.get("video_id") != job:
                        raise MediaUnknown("HeyGen returned a receipt for another video")
                if data.get("status") in {"cancelled", "failed"}:
                    raise VideoRejected("HeyGen video generation did not complete")
                if data.get("status") != "completed":
                    raise RuntimeError("HeyGen video returned an unknown status")
                await dispatch.authorize_http()
                video = await _download(client, data.get("video_url"))
        except TimeoutError:
            raise RuntimeError("HeyGen video wait timed out; submission will not be repeated") from None
        duration = data.get("duration")
        if not isinstance(duration, int | float) or not 0 < duration <= request.duration_s:
            duration = request.duration_s
        return video, {
            "model": model, "model_served": model, "mime_type": "video/mp4",
            "tokens_in": 0, "tokens_out": 0, "cost_usd": estimate_heygen_cost(request),
            "video_s": float(duration), "latency_ms": int((time.monotonic() - started) * 1000),
        }
