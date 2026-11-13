"""Short video clips (`video` capability, POST /v1/video/jobs).

Gemini Omni Flash on PAID keys only (the model has no free tier, and a clip costs
cents to a dollar). A key must also carry the `llm:video` scope, so the lane is
opt-in per key as well as per project.

Always asynchronous: a generation takes tens of seconds to minutes, so the route
only enqueues a job and the dispatcher (services/job_queue.py) runs `run_video`.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from aibroker.db.models import ProjectRow
from aibroker.providers.catalog import resolve_pin
from aibroker.providers.gemini_video import (
    VideoRejected,
    VideoRequest,
    estimate_video_cost,
)
from aibroker.providers.registry import model_for
from aibroker.providers.transport import generate_video
from aibroker.routing import chain_for, pick_and_reserve, scope_for
from aibroker.services.attempt import Attempt, Flow, run_attempt
from aibroker.services.llm_service import (
    _BudgetExhausted,
    _exhausted_for,
    _failure_text,
    _max_keys,
)

CAPABILITY = "video"
# Keys of the queued request (deep_jobs.request) that carry a video job.
VIDEO_FIELD = "video"
# Dispatcher retries for a video job: a clip is billed per generation, so a failing
# job must not be re-run the eight times a chat job is.
VIDEO_MAX_RETRIES = 2


@dataclass
class VideoOutcome:
    """A finished clip. `text` is the MP4 as base64: the job row stores text, and
    the poll route hands it back as `video_base64`."""
    text: str
    mime_type: str
    video_s: float
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int
    model_served: str | None = None


class VideoFailed(Exception):
    """Every paid key failed (the job is retried, then ends in error)."""


def _generate_call(model: str, request: VideoRequest):
    async def call(plain: str):
        return await generate_video(model=model, request=request, api_key=plain)
    return call


def video_job_payload(request: VideoRequest) -> dict[str, Any]:
    """The JSON-safe form of a request for the job row (bytes -> base64)."""
    return {
        "prompt": request.prompt, "duration_s": request.duration_s,
        "aspect_ratio": request.aspect_ratio, "resolution": request.resolution,
        "image_b64": base64.b64encode(request.image).decode() if request.image else None,
        "image_mime": request.image_mime,
    }


def video_request_from_job(payload: dict[str, Any]) -> VideoRequest:
    """Inverse of video_job_payload (a KeyError on a malformed row fails the job)."""
    image_b64 = payload.get("image_b64")
    return VideoRequest(
        prompt=str(payload["prompt"]), duration_s=int(payload["duration_s"]),
        aspect_ratio=str(payload["aspect_ratio"]), resolution=str(payload["resolution"]),
        image=base64.b64decode(str(image_b64)) if image_b64 else None,
        image_mime=str(payload["image_mime"]) if payload.get("image_mime") else None,
    )


async def run_video(
    *,
    project: ProjectRow,
    request: VideoRequest,
    model: str | None,
    workflow: str | None,
) -> VideoOutcome | _BudgetExhausted | None:
    """Prompt → clip, rotating the provider's PAID keys that carry llm:video.

    None → no such key right now (the job is retried); the budget sentinel → a
    project/global cap is spent; VideoFailed → every key tried and failed;
    VideoRejected → the request itself was refused, no other key is tried."""
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
            estimated_cost=estimate_video_cost(use_model, request),
            call=_generate_call(use_model, request),
            cap_flow=Flow.NEXT_KEY, pinned_model=model, note_affinity=False,
        ))
        if res.flow is Flow.SUCCESS:
            meta = res.meta
            return VideoOutcome(
                text=base64.b64encode(res.payload).decode(),
                mime_type=meta.get("mime_type") or "video/mp4",
                video_s=float(meta.get("video_s") or 0.0),
                provider=provider, model=use_model,
                tokens_in=meta.get("tokens_in", 0), tokens_out=meta.get("tokens_out", 0),
                cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
                key_label=key.label, request_id=res.usage_id or 0,
                model_served=meta.get("model_served"),
            )
        if isinstance(res.error, VideoRejected):
            raise res.error
        if res.flow is Flow.BUDGET_EXHAUSTED:
            return _exhausted_for(res.error)
        last_error = _failure_text(res)
    if not any_key_seen:
        return None
    raise VideoFailed(last_error or "all keys failed")
