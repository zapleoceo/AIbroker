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
from aibroker.routing import scope_for
from aibroker.services import (
    EmbedFailed,
    TranscribeFailed,
    get_job,
    next_poll_after_s,
    run_embed,
    run_transcribe,
    submit_deep_job,
    submit_job,
)

# Capabilities the async job API serves — everything run_chat handles. embed
# and transcription stay sync-only (fast, no held-connection problem async
# solves). chat:deep is included (it's a run_chat capability) and is the one
# capability that is async-ONLY.
_JOB_CAPABILITIES = frozenset({
    "chat:fast", "chat:smart", "chat:code", "chat:edit", "chat:deep",
    "structured", "prefilter", "translate", "vision",
})

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


@router.post("/chat")
async def chat_removed(capability: str = Query("chat:fast")) -> None:
    """Sync chat was removed (2026-07-10) — the broker is async-only for chat.
    A slow/oversubscribed provider could 504 a synchronous call before the
    fallback chain finished; the async job queue has no such ceiling and
    exhaustively rotates keys. Kept as a `410 Gone` with a migration hint so a
    caller still on the old endpoint gets a clear signal, not a bare 404.

    (embed/transcribe stay synchronous — they're fast and never hit the proxy
    read-timeout that async solves; see docs/api.md.)"""
    raise HTTPException(
        status.HTTP_410_GONE,
        f"sync /v1/chat is removed — POST /v1/jobs?capability={capability} to "
        "submit, GET /v1/jobs/{job_id} to poll (see docs/api.md).",
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


def _job_response(row: Any) -> DeepJobResponse:  # pragma: no cover
    """Shape a deep_jobs row into the poll response — shared by /v1/deep and
    /v1/jobs. pending/error/done branches read a row inserted by a SEPARATE
    session/request; cross-session reads don't see it on SQLite (see
    deep_jobs.get_job), so these are Postgres-only-tested (skipif ON_SQLITE)."""
    if row.status == "pending":
        return DeepJobResponse(
            job_id=row.id, status="pending",
            poll_after_s=next_poll_after_s(row.created_at),
        )
    if row.status == "error":
        return DeepJobResponse(job_id=row.id, status="error", error=row.error_message)
    meta = row.result_meta or {}
    return DeepJobResponse(
        job_id=row.id, status="done", text=row.result_text,
        provider=meta.get("provider"), model=meta.get("model"),
        tokens_in=meta.get("tokens_in"), tokens_out=meta.get("tokens_out"),
        cost_usd=meta.get("cost_usd"), latency_ms=meta.get("latency_ms"),
        key_label=meta.get("key_label"), request_id=meta.get("request_id"),
    )


@router.get("/deep/{job_id}", response_model=DeepJobResponse)
async def deep_poll(
    job_id: int,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepJobResponse:
    row = await get_job(job_id, ctx.project.id)
    if row is None:
        raise HTTPException(404, "job not found")
    return _job_response(row)  # pragma: no cover


# ─── Generic async jobs — submit+poll for ANY chat capability (Phase 4) ─────
#
# Same submit/poll shape as /v1/deep, opened to every chat capability so
# clients (Vera, Stepan) can migrate off the sync /v1/chat endpoint at their
# own pace: they get a guaranteed answer (exhaustive rotation, no held
# connection that a slow provider could 504). Sync /v1/chat stays — additive,
# backward-compatible. See docs/routing.md and services/deep_jobs.py.


class JobSubmitResponse(BaseModel):
    job_id: int
    status: str = "pending"
    poll_url: str
    poll_after_s: int = Field(description="suggested wait before the first poll")


@router.post("/jobs", response_model=JobSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def jobs_submit(
    body: ChatRequest,
    capability: str = Query("chat:fast"),
    ctx: ProjectCtx = Depends(require_project),
) -> JobSubmitResponse:
    """Submit any chat `capability` as an async job. Returns a job_id
    immediately — poll GET /v1/jobs/{job_id}. Mirrors POST /v1/chat's body
    (incl. response_format), but never holds the connection."""
    if capability not in _JOB_CAPABILITIES:
        raise HTTPException(
            400,
            f"capability={capability} is not available as an async job "
            f"(sync-only or unknown). Async job capabilities: "
            f"{sorted(_JOB_CAPABILITIES)}",
        )
    _require_capability_scope(ctx, scope_for(capability))  # type: ignore[arg-type]
    job_id = await submit_job(  # pragma: no cover
        project=ctx.project, capability=capability,
        messages=[m.model_dump() for m in body.messages],
        model=body.model, max_tokens=body.max_tokens, temperature=body.temperature,
        response_format=body.response_format, workflow=body.workflow,
    )
    return JobSubmitResponse(  # pragma: no cover
        job_id=job_id, poll_url=f"/v1/jobs/{job_id}", poll_after_s=2,
    )


@router.get("/jobs/{job_id}", response_model=DeepJobResponse)
async def jobs_poll(
    job_id: int,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepJobResponse:
    row = await get_job(job_id, ctx.project.id)
    if row is None:
        raise HTTPException(404, "job not found")
    return _job_response(row)  # pragma: no cover
