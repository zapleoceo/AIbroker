"""LLM proxy mode — broker calls provider with its key, returns response.

Endpoint: POST /v1/chat?capability=chat:fast
Endpoint: POST /v1/embed?provider=voyage
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from aibroker.auth import ProjectCtx, require_scope
from aibroker.config import get_settings
from aibroker.crypto import decrypt
from aibroker.providers import call_llm
from aibroker.providers.litellm_adapter import embed, model_for
from aibroker.routing import CostGuardError, chain_for, check_caps, pick_and_reserve
from aibroker.routing.selector import mark_cooldown, mark_dead, record_usage
from aibroker.telemetry import audit
from aibroker.db.models import ApiKeyRow, ProjectRow

router = APIRouter(tags=["proxy"])

log = logging.getLogger(__name__)


# ─── Schemas ────────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    content: str


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


# ─── Helpers ────────────────────────────────────────────────────────────────


async def _try_one_provider(
    provider: str,
    project: ProjectRow,
    capability: str,
    body: ChatRequest,
    workflow: str | None,
) -> tuple[ApiKeyRow, str, dict[str, Any]] | None:
    """Pick a key for provider, call LiteLLM. Return (key, text, meta) or None to fallback."""
    s = get_settings()
    key = await pick_and_reserve(provider, scope="llm:chat")
    if key is None:
        return None

    # Pre-flight: cap check with a cheap estimate (0 — actual cost computed after call)
    try:
        await check_caps(api_key=key, project=project, estimated_cost=0.0)
    except CostGuardError as e:
        await audit(actor=f"project:{project.name}", action="cap_block",
                    target=f"provider={provider}", metadata={"reason": str(e)})
        return None

    model = body.model or model_for(provider, capability)
    if not model:
        return None

    plain = decrypt(key.token_encrypted)
    try:
        text, meta = await call_llm(
            model=model,
            messages=[m.model_dump() for m in body.messages],
            api_key=plain,
            max_tokens=body.max_tokens,
            temperature=body.temperature,
            response_format=body.response_format,
        )
    except Exception as e:
        emsg = str(e).lower()
        if "rate_limit" in emsg or "429" in emsg:
            await mark_cooldown(key.id, datetime.now(timezone.utc) + timedelta(minutes=5))
        elif "401" in emsg or "403" in emsg or "auth" in emsg:
            await mark_dead(key.id)
        await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=model, capability=capability, workflow=workflow,
            tokens_in=0, tokens_out=0, cost_usd=0.0, latency_ms=None,
            status="error", error_kind=type(e).__name__, http_status=None,
        )
        log.warning("provider %s failed: %s", provider, e)
        return None

    await record_usage(
        api_key_id=key.id, project_id=project.id, lease_id=None,
        provider=provider, model=model, capability=capability, workflow=workflow,
        tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
        cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
        status="ok", error_kind=None, http_status=200,
    )
    return key, text, meta


# ─── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    capability: str = Query("chat:fast"),
    ctx: ProjectCtx = Depends(require_scope("llm:chat")),
) -> ChatResponse:
    if capability not in {"chat:fast", "chat:smart", "chat:code", "prefilter",
                          "structured", "vision"}:
        raise HTTPException(400, f"unknown capability: {capability}")
    for provider in chain_for(capability):  # type: ignore[arg-type]
        result = await _try_one_provider(provider, ctx.project, capability, body, body.workflow)
        if result is None:
            continue
        key, text, meta = result
        return ChatResponse(
            text=text, provider=key.provider, model=meta["model"],
            tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
        )
    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"no provider available for capability={capability}",
    )


@router.post("/embed", response_model=EmbedResponse)
async def embed_endpoint(
    body: EmbedRequest,
    provider: str = Query("voyage"),
    ctx: ProjectCtx = Depends(require_scope("llm:embed")),
) -> EmbedResponse:
    key = await pick_and_reserve(provider, scope="llm:embed")
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"no embedding key available for provider={provider}",
        )
    model = body.model or model_for(provider, "embedding") or "voyage/voyage-3"
    plain = decrypt(key.token_encrypted)
    try:
        vectors, meta = await embed(model=model, texts=body.input, api_key=plain)
    except Exception as e:
        emsg = str(e).lower()
        if "rate_limit" in emsg or "429" in emsg:
            await mark_cooldown(key.id, datetime.now(timezone.utc) + timedelta(minutes=5))
        elif "401" in emsg or "403" in emsg:
            await mark_dead(key.id)
        await record_usage(
            api_key_id=key.id, project_id=ctx.project.id, lease_id=None,
            provider=provider, model=model, capability="embedding",
            workflow=body.workflow, tokens_in=0, tokens_out=0, cost_usd=0,
            latency_ms=None, status="error", error_kind=type(e).__name__,
            http_status=None,
        )
        raise HTTPException(502, f"embed failed: {e}") from e

    await record_usage(
        api_key_id=key.id, project_id=ctx.project.id, lease_id=None,
        provider=provider, model=model, capability="embedding",
        workflow=body.workflow, tokens_in=meta["tokens_in"], tokens_out=0,
        cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
        status="ok", error_kind=None, http_status=200,
    )
    return EmbedResponse(
        embeddings=vectors, provider=provider, model=model,
        tokens_in=meta["tokens_in"], cost_usd=meta["cost_usd"],
        latency_ms=meta["latency_ms"],
    )
