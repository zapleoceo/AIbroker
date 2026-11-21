"""POST /v1/images/jobs — prompt + reference images -> one generated image.

Async only, the /v1/video/jobs pattern: the route validates and enqueues, the
dispatcher runs services/image_service.run_image on a PAID gemini key carrying
`llm:image`, and the caller polls GET /v1/jobs/{id} for `image_base64`.
"""
from __future__ import annotations

import base64
import binascii
import io
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from aibroker.auth import ProjectCtx, require_project
from aibroker.providers.gemini_image import IMAGE_ASPECT_RATIOS, IMAGE_SIZES, ImageRequest
from aibroker.routes.jobs_control import idempotency_conflict
from aibroker.routes.proxy import (
    JobSubmitResponse,
    _replay,
    _require_capability_scope,
    _validate_pin,
)
from aibroker.routing import scope_for
from aibroker.routing.key_tier import KeyTier, paid_lane
from aibroker.services.deep_jobs import IdempotencyConflict, resolve_priority, submit_job_ex
from aibroker.services.image_service import IMAGE_FIELD, image_job_payload
from aibroker.services.job_meta import check_labels, check_token, resolve_trace_id
from aibroker.services.request_cap import admit_request
from aibroker.telemetry.request_context import REQUEST_ID_HEADER, job_request_id

router = APIRouter(tags=["images"])

IMAGE_MAX_PROMPT_CHARS = 8000
IMAGE_MAX_REFERENCES = 8
# References ride in the job row as base64. The body is capped at 28 MB by nginx.
IMAGE_MAX_REFERENCE_BYTES = 7 * 1024 * 1024
IMAGE_MAX_REFERENCES_BYTES = 20 * 1024 * 1024
_FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}


class ImageReference(BaseModel):
    image_base64: str = Field(min_length=1, description="base64 (a data: URL prefix is accepted)")
    mime: Literal["image/jpeg", "image/png", "image/webp"] | None = Field(
        None, description="optional: the type is read from the bytes")


class ImageJobRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=IMAGE_MAX_PROMPT_CHARS)
    references: list[ImageReference] = Field(
        default_factory=list, max_length=IMAGE_MAX_REFERENCES,
        description="reference images (identity, outfit, scene), sent in order before the prompt")
    aspect_ratio: Literal[IMAGE_ASPECT_RATIOS] | None = Field(  # type: ignore[valid-type]
        None, description="null: the model decides (usually follows the first reference)")
    image_size: Literal[IMAGE_SIZES] = "1K"  # type: ignore[valid-type]
    model: str | None = Field(None, description="override the lane model "
                                                "(gemini-3.1-flash-image by default)")
    workflow: str | None = None
    trace_id: str | None = None
    labels: dict[str, str] | None = None
    priority: Literal["interactive", "batch"] | None = None
    # docs/api.md "Key tier": image runs on paid keys only, so "free" is rejected.
    key_tier: KeyTier = "auto"

    @field_validator("key_tier")
    @classmethod
    def _paid_lane_tier(_cls, v: str) -> str:
        return paid_lane(v, "image")

    @field_validator("prompt")
    @classmethod
    def _nonblank(_cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must not be blank")
        return v

    @field_validator("trace_id")
    @classmethod
    def _trace(_cls, value: str | None) -> str | None:
        return None if value is None else check_token(value, "trace_id")

    @field_validator("labels")
    @classmethod
    def _labels(_cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return check_labels(value)


def decode_references(refs: list[ImageReference]) -> tuple[tuple[bytes, str], ...]:
    """(bytes, mime) per reference, the mime read from the bytes; ValueError names the
    first bad one (-> 400)."""
    from PIL import Image

    out: list[tuple[bytes, str]] = []
    total = 0
    for n, ref in enumerate(refs, 1):
        encoded = ref.image_base64
        if encoded.startswith("data:") and "base64," in encoded[:100]:
            encoded = encoded.split("base64,", 1)[1]
        if len(encoded) > 4 * ((IMAGE_MAX_REFERENCE_BYTES + 2) // 3):
            raise ValueError(f"reference #{n} exceeds "
                             f"{IMAGE_MAX_REFERENCE_BYTES // (1024 * 1024)} MB")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"reference #{n} is not valid base64") from e
        total += len(raw)
        if total > IMAGE_MAX_REFERENCES_BYTES:
            raise ValueError(f"references exceed "
                             f"{IMAGE_MAX_REFERENCES_BYTES // (1024 * 1024)} MB in total")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                fmt = image.format
        except Exception as e:  # noqa: BLE001 — any Pillow failure means "not an image"
            raise ValueError(f"reference #{n} is not a decodable image") from e
        mime = _FORMAT_MIME.get(fmt or "")
        if mime is None:
            raise ValueError(f"reference #{n} is {fmt}; accepted: JPEG, PNG, WebP")
        out.append((raw, mime))
    return tuple(out)


@router.post("/images/jobs", response_model=JobSubmitResponse,
             status_code=status.HTTP_202_ACCEPTED,
             responses={200: {"description": "Idempotent-Replay: same Idempotency-Key + payload"},
                        409: {"description": "idempotency_key_conflict"}})
async def images_submit(
    body: ImageJobRequest,
    response: Response,
    idempotency_key: Annotated[str | None, Header(
        description="<=128 chars [A-Za-z0-9._:-]; same key + payload returns the same job")] = None,
    x_trace_id: Annotated[str | None, Header(description="alternative to body `trace_id`")] = None,
    ctx: ProjectCtx = Depends(require_project),
) -> JobSubmitResponse | JSONResponse:
    """Prompt + up to 8 reference images -> one image, ASYNC ONLY: returns a job_id at
    once, poll GET /v1/jobs/{id}; a finished job carries `image_base64` + `mime_type`.

    Gemini image models (gemini-3.1-flash-image by default) on paid keys carrying
    `llm:image`. A refused prompt ends the job in `error` without retries; each
    finished image is billed (`cost_usd`)."""
    _require_capability_scope(ctx, scope_for("image"))
    try:
        trace_id = resolve_trace_id(body.trace_id, x_trace_id)
        if idempotency_key is not None:
            check_token(idempotency_key, "Idempotency-Key")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _validate_pin(body.model, "image")
    try:
        references = decode_references(body.references)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    await admit_request(ctx.project)
    request = ImageRequest(prompt=body.prompt, references=references,
                           aspect_ratio=body.aspect_ratio, image_size=body.image_size)
    try:
        outcome = await submit_job_ex(
            project=ctx.project, capability="image",
            messages=[], model=body.model, max_tokens=0, temperature=0.0,
            response_format=None, workflow=body.workflow,
            extra={IMAGE_FIELD: image_job_payload(request)},
            trace_id=trace_id, labels=body.labels, idempotency_key=idempotency_key,
            priority=resolve_priority(body.priority, body.labels),
        )
    except IdempotencyConflict as exc:
        raise idempotency_conflict(exc) from exc
    if outcome.replayed:
        return await _replay(ctx, outcome.job_id)
    response.headers[REQUEST_ID_HEADER] = job_request_id(outcome.job_id)
    return JobSubmitResponse(job_id=outcome.job_id, poll_url=f"/v1/jobs/{outcome.job_id}",
                             poll_after_s=5, trace_id=trace_id, labels=body.labels)
