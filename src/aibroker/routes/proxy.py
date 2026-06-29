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
    run_chat,
    run_embed,
    run_transcribe,
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
    )


class TranscribeResponse(BaseModel):
    text: str
    provider: str
    model: str
    cost_usd: float
    latency_ms: int
    key_label: str


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
        key_label=outcome.key_label,
    )
