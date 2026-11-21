"""Image generation from a prompt + reference images (`image` capability,
POST /v1/images/jobs).

Gemini image models (Nano Banana) on PAID keys only, and a key must also carry the
`llm:image` scope, so the lane is opt-in per key as well as per project. The Codex
subscription image route (POST /v1/jobs with a codex-sub model) is separate.

Always asynchronous: the route only enqueues a job and the dispatcher
(services/job_queue.py) runs `run_image`. One generateContent call has no remote
job id to resume, so a worker that dies after Google answered re-runs the call
(one more image billed); IMAGE_MAX_RETRIES bounds that and every other retry.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

from aibroker.db.models import ProjectRow
from aibroker.providers.catalog import resolve_pin
from aibroker.providers.gemini_image import ImageRejected, ImageRequest, estimate_image_cost
from aibroker.providers.registry import model_for
from aibroker.providers.transport import generate_image
from aibroker.routing import chain_for, pick_and_reserve, scope_for
from aibroker.services.attempt import Attempt, Flow, run_attempt
from aibroker.services.llm_service import (
    _BudgetExhausted,
    _exhausted_for,
    _failure_text,
    _max_keys,
)

CAPABILITY = "image"
# Key of the queued request (deep_jobs.request) that carries an image job.
IMAGE_FIELD = "image"
# Dispatcher retries for an image job: every finished image is billed.
IMAGE_MAX_RETRIES = 2


@dataclass
class ImageOutcome:
    """A finished image. `text` is the image as base64: the job row stores text, and
    the poll route hands it back as `image_base64`."""
    text: str
    mime_type: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int
    model_served: str | None = None


class ImageFailed(Exception):
    """Every paid key failed (the job is retried, then ends in error)."""


def _generate_call(model: str, request: ImageRequest):
    async def call(plain: str):
        return await generate_image(model=model, request=request, api_key=plain)
    return call


def image_job_payload(request: ImageRequest) -> dict[str, Any]:
    """The JSON-safe form of a request for the job row (bytes -> base64)."""
    return {
        "prompt": request.prompt,
        "references": [{"data_b64": base64.b64encode(raw).decode(), "mime": mime}
                       for raw, mime in request.references],
        "aspect_ratio": request.aspect_ratio, "image_size": request.image_size,
    }


def image_request_from_job(payload: dict[str, Any]) -> ImageRequest:
    """Inverse of image_job_payload (a KeyError on a malformed row fails the job)."""
    return ImageRequest(
        prompt=str(payload["prompt"]),
        references=tuple((base64.b64decode(str(r["data_b64"])), str(r["mime"]))
                         for r in payload.get("references") or []),
        aspect_ratio=payload.get("aspect_ratio"),
        image_size=str(payload.get("image_size") or "1K"),
    )


async def run_image(
    *,
    project: ProjectRow,
    request: ImageRequest,
    model: str | None,
    workflow: str | None,
) -> ImageOutcome | _BudgetExhausted | None:
    """Prompt (+ references) -> image, rotating the provider's PAID keys that carry
    llm:image.

    None -> no such key right now (the job is retried); the budget sentinel -> a
    project/global cap is spent; ImageFailed -> every key tried and failed;
    ImageRejected -> the request itself was refused, no other key is tried."""
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
            estimated_cost=estimate_image_cost(use_model, request),
            call=_generate_call(use_model, request),
            cap_flow=Flow.NEXT_KEY, pinned_model=model, note_affinity=False,
        ))
        if res.flow is Flow.SUCCESS:
            meta = res.meta
            return ImageOutcome(
                text=base64.b64encode(res.payload).decode(),
                mime_type=meta.get("mime_type") or "image/png",
                provider=provider, model=use_model,
                tokens_in=meta.get("tokens_in", 0), tokens_out=meta.get("tokens_out", 0),
                cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
                key_label=key.label, request_id=res.usage_id or 0,
                model_served=meta.get("model_served"),
            )
        if isinstance(res.error, ImageRejected):
            raise res.error
        if res.flow is Flow.BUDGET_EXHAUSTED:
            return _exhausted_for(res.error)
        last_error = _failure_text(res)
    if not any_key_seen:
        return None
    raise ImageFailed(last_error or "all keys failed")
