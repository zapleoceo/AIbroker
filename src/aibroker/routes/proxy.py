"""LLM proxy mode — broker calls the provider with its key, returns the response.

Endpoint: POST /v1/chat?capability=chat:fast
Endpoint: POST /v1/embed?provider=voyage

Thin layer: authenticate, gate on the capability's scope, delegate to
services.llm_service, shape the response. All orchestration lives in the service.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from aibroker.auth import ProjectCtx, require_project
from aibroker.routing import is_known_capability, scope_for
from aibroker.services import (
    EmbedFailed,
    TranscribeFailed,
    get_job,
    next_poll_after_s,
    run_chat,
    run_embed,
    run_transcribe,
    submit_deep_job,
)

router = APIRouter(tags=["proxy"])

log = logging.getLogger(__name__)


# ─── Schemas ────────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    # str for plain text; list[dict] for OpenAI-style multimodal content
    # blocks (e.g. [{"type":"text",...}, {"type":"image_url",...}]). LiteLLM
    # passes both shapes through to vision-capable models natively.
    content: str | list[dict[str, Any]]


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = Field(None, description="override provider's default model")
    max_tokens: int = 1024
    temperature: float = 0.7
    response_format: dict[str, Any] | None = None
    workflow: str | None = None


class ChatResponse(BaseModel):
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int = Field(
        description="usage_log.id for this call. Log it — quote it back to us "
                     "to find the exact call in this broker's dashboard/DB."
    )
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class EmbedRequest(BaseModel):
    input: list[str] = Field(min_length=1, max_length=128)
    model: str | None = None
    workflow: str | None = None


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    provider: str
    model: str
    tokens_in: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int = Field(description="usage_log.id for this call.")


# ─── Helpers ────────────────────────────────────────────────────────────────


def _require_capability_scope(ctx: ProjectCtx, scope: str) -> None:
    if not ctx.has_scope(scope):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"project lacks scope: {scope}")


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    capability: str = Query("chat:fast"),
    ctx: ProjectCtx = Depends(require_project),
) -> ChatResponse:
    if not is_known_capability(capability):
        raise HTTPException(400, f"unknown capability: {capability}")
    if capability == "chat:deep":
        # Real latency has been observed up to ~8 minutes on the free NVIDIA
        # pool — well past Cloudflare's and this broker's own nginx read
        # timeouts. A synchronous call here reliably 504s the caller while
        # the broker is still waiting on the provider. Use the async job API.
        raise HTTPException(
            400,
            "capability=chat:deep is async-only — POST /v1/deep to submit, "
            "GET /v1/deep/{job_id} to poll (see docs/routing.md)",
        )
    _require_capability_scope(ctx, scope_for(capability))  # type: ignore[arg-type]

    outcome = await run_chat(
        project=ctx.project, capability=capability,
        messages=[m.model_dump() for m in body.messages],
        model=body.model, max_tokens=body.max_tokens, temperature=body.temperature,
        response_format=body.response_format, workflow=body.workflow,
    )
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"no provider available for capability={capability}",
        )
    return ChatResponse(
        text=outcome.text, provider=outcome.provider, model=outcome.model,
        tokens_in=outcome.tokens_in, tokens_out=outcome.tokens_out,
        cost_usd=outcome.cost_usd, latency_ms=outcome.latency_ms,
        key_label=outcome.key_label,
        cache_read_tokens=outcome.cache_read_tokens,
        cache_write_tokens=outcome.cache_write_tokens,
        request_id=outcome.request_id,
    )


@router.post("/embed", response_model=EmbedResponse)
async def embed_endpoint(
    body: EmbedRequest,
    provider: str = Query("voyage"),
    ctx: ProjectCtx = Depends(require_project),
) -> EmbedResponse:
    _require_capability_scope(ctx, scope_for("embedding"))

    try:
        outcome = await run_embed(
            project=ctx.project, provider=provider,
            inputs=body.input, model=body.model, workflow=body.workflow,
        )
    except EmbedFailed as e:
        raise HTTPException(502, f"embed failed: {e}") from e
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"no embedding key available for provider={provider}",
        )
    return EmbedResponse(
        embeddings=outcome.embeddings, provider=outcome.provider, model=outcome.model,
        tokens_in=outcome.tokens_in, cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms, key_label=outcome.key_label,
        request_id=outcome.request_id,
    )


class TranscribeResponse(BaseModel):
    text: str
    provider: str
    model: str
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int = Field(description="usage_log.id for this call.")


# 25 MB — Whisper's hard limit at both Groq and OpenAI.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(
    file: UploadFile = File(...),
    workflow: str | None = Query(None),
    ctx: ProjectCtx = Depends(require_project),
) -> TranscribeResponse:
    """Audio → text. Multipart upload `file`. Chain: groq whisper → openai."""
    _require_capability_scope(ctx, scope_for("transcription"))

    audio = await file.read()
    if not audio:
        raise HTTPException(400, "empty audio file")
    if len(audio) > _MAX_AUDIO_BYTES:
        raise HTTPException(413, f"audio exceeds {_MAX_AUDIO_BYTES // (1024 * 1024)} MB")

    try:
        outcome = await run_transcribe(
            project=ctx.project, audio=audio,
            filename=file.filename or "audio.ogg", workflow=workflow,
        )
    except TranscribeFailed as e:
        raise HTTPException(502, f"transcription failed: {e}") from e
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no transcription key available",
        )
    return TranscribeResponse(
        text=outcome.text, provider=outcome.provider, model=outcome.model,
        cost_usd=outcome.cost_usd, latency_ms=outcome.latency_ms,
        key_label=outcome.key_label, request_id=outcome.request_id,
    )


# ─── chat:deep — async job API (see services/deep_jobs.py for why) ─────────


class DeepRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = Field(None, description="override provider's default model")
    max_tokens: int = 4096
    temperature: float = 0.7
    workflow: str | None = None


class DeepSubmitResponse(BaseModel):
    job_id: int
    status: str = "pending"
    poll_url: str
    poll_after_s: int = Field(description="suggested wait before the first poll")


class DeepJobResponse(BaseModel):
    job_id: int
    status: str = Field(description="pending|done|error")
    text: str | None = None
    provider: str | None = None
    model: str | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    key_label: str | None = None
    request_id: int | None = None
    error: str | None = None
    poll_after_s: int | None = Field(
        None, description="present only while status=pending"
    )


@router.post("/deep", response_model=DeepSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def deep_submit(
    body: DeepRequest,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepSubmitResponse:
    """Submit a chat:deep (long-context/reasoning, 1M-token nemotron) request.
    Returns immediately with a job_id — poll GET /v1/deep/{job_id} for the
    result. Real latency has been observed up to ~8 minutes."""
    _require_capability_scope(ctx, scope_for("chat:deep"))
    # submit_deep_job needs a real autoincrementing BIGSERIAL id — SQLite
    # doesn't do that for BigInteger, so this whole path is exercised only by
    # the Postgres-only test_deep_submit_creates_job_and_runs_in_background.
    job_id = await submit_deep_job(  # pragma: no cover
        project=ctx.project,
        messages=[m.model_dump() for m in body.messages],
        model=body.model, max_tokens=body.max_tokens, temperature=body.temperature,
        workflow=body.workflow,
    )
    return DeepSubmitResponse(  # pragma: no cover
        job_id=job_id, poll_url=f"/v1/deep/{job_id}", poll_after_s=5,
    )


@router.get("/deep/{job_id}", response_model=DeepJobResponse)
async def deep_poll(
    job_id: int,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepJobResponse:
    row = await get_job(job_id, ctx.project.id)
    if row is None:
        raise HTTPException(404, "job not found")
    # pending/error/done branches read a row inserted by a SEPARATE
    # session/request — cross-session reads on SQLite don't see it (see
    # deep_jobs.get_job) — so these are Postgres-only-tested (skipif
    # ON_SQLITE): test_deep_poll_pending_job_returns_poll_after_s,
    # test_deep_poll_error_job_returns_error_message,
    # test_deep_poll_done_job_returns_result_meta.
    if row.status == "pending":  # pragma: no cover
        return DeepJobResponse(
            job_id=row.id, status="pending",
            poll_after_s=next_poll_after_s(row.created_at),
        )
    if row.status == "error":  # pragma: no cover
        return DeepJobResponse(job_id=row.id, status="error", error=row.error_message)
    meta = row.result_meta or {}  # pragma: no cover
    return DeepJobResponse(  # pragma: no cover
        job_id=row.id, status="done", text=row.result_text,
        provider=meta.get("provider"), model=meta.get("model"),
        tokens_in=meta.get("tokens_in"), tokens_out=meta.get("tokens_out"),
        cost_usd=meta.get("cost_usd"), latency_ms=meta.get("latency_ms"),
        key_label=meta.get("key_label"), request_id=meta.get("request_id"),
    )
