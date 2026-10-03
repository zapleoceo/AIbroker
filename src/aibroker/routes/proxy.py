"""LLM proxy mode — broker calls the provider with its key, returns the response.

Endpoint: POST /v1/jobs?capability=chat:fast (async chat; POST /v1/chat is a 410)
Endpoint: POST /v1/embed?provider=voyage

Thin layer: authenticate, gate on the capability's scope, delegate to
services.llm_service, shape the response. All orchestration lives in the service.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status
from pydantic import BaseModel, Field, model_validator

from aibroker.auth import ProjectCtx, require_project
from aibroker.providers import catalog
from aibroker.providers.catalog import UnknownModel
from aibroker.providers.decisions import DecisionRequestInvalid, validate_questions
from aibroker.routing import chain_for, scope_for
from aibroker.services import (
    DecisionFailed,
    EmbedFailed,
    EmbedRequestInvalid,
    TranscribeFailed,
    get_job,
    next_poll_after_s,
    run_decision,
    run_embed,
    run_transcribe,
    submit_deep_job,
    submit_job,
)
from aibroker.services.deep_jobs import AUDIO_FIELD
from aibroker.services.request_cap import admit_request
from aibroker.services.tool_contract import ToolDefinition, tool_model_provider, validate_choice
from aibroker.services.vision_payload import inline_image_problem
from aibroker.telemetry.request_context import REQUEST_ID_HEADER, job_request_id

# Capabilities the generic /v1/jobs endpoint serves — everything run_chat
# handles, i.e. everything whose payload is chat messages. embed is
# sync-only (fast, no held-connection problem to solve). TRANSCRIPTION is
# async too, but through its own multipart route (/v1/transcribe/jobs) since
# its payload is an audio file, not messages — it is polled via the same
# GET /v1/jobs/{id}. chat:deep is the one capability that is async-ONLY.
_JOB_CAPABILITIES = frozenset({
    "chat:fast", "chat:smart", "chat:sales", "chat:code", "chat:edit",
    "chat:deep", "structured", "prefilter", "translate", "vision",
})

router = APIRouter(tags=["proxy"])

log = logging.getLogger(__name__)


# ─── Schemas ────────────────────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    # str for plain text; list[dict] for OpenAI-style multimodal content
    # blocks (e.g. [{"type":"text",...}, {"type":"image_url",...}]). LiteLLM
    # passes both shapes through to vision-capable models natively.
    content: str | list[dict[str, Any]] | None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def valid_tool_message(self) -> ChatMessage:
        if self.content is None and not (self.role == "assistant" and self.tool_calls):
            raise ValueError("null content requires assistant tool_calls")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool_calls require assistant role")
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("tool message requires tool_call_id")
        if self.tool_call_id and self.role != "tool":
            raise ValueError("tool_call_id requires tool role")
        return self


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = Field(None, description="override provider's default model")
    # Bounded (2026-07-16): an oversized max_tokens inflates the cost-guard's
    # worst-case reservation estimate (est_tokens × max_tokens pricing) and
    # silently knocks every capped paid key out of the chain — the paid tail
    # vanishes and the request 503s with keys sitting idle. 16384 covers every
    # real caller; temperature beyond [0, 2] is rejected by providers anyway.
    max_tokens: int = Field(1024, ge=1, le=16384)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
    response_format: dict[str, Any] | None = None
    workflow: str | None = None
    tools: list[ToolDefinition] | None = Field(None, min_length=1, max_length=32)
    tool_choice: str | dict[str, Any] | None = None

    @model_validator(mode="after")
    def valid_tools(self) -> ChatRequest:
        if self.tools:
            tool_model_provider(self.model)
            if self.response_format:
                raise ValueError("tools and response_format are mutually exclusive")
            validate_choice([tool.model_dump(exclude_none=True) for tool in self.tools],
                            self.tool_choice)
        elif self.tool_choice is not None:
            raise ValueError("tool_choice requires tools")
        pending: set[str] = set()
        seen: set[str] = set()
        for message in self.messages:
            if message.role == "tool":
                if message.tool_call_id is None or message.tool_call_id not in pending:
                    raise ValueError("tool result must match an outstanding call")
                pending.remove(message.tool_call_id)
            else:
                if pending:
                    raise ValueError("all tool results must precede the next message")
                for call in message.tool_calls or []:
                    call_id = call.get("id")
                    function = call.get("function")
                    if (not isinstance(call_id, str) or not call_id or call_id in seen
                            or call.get("type") != "function" or not isinstance(function, dict)
                            or not isinstance(function.get("name"), str)
                            or not isinstance(function.get("arguments"), str)):
                        raise ValueError("invalid assistant tool-call history")
                    pending.add(call_id)
                    seen.add(call_id)
        if pending:
            raise ValueError("missing tool results")
        return self


class EmbedRequest(BaseModel):
    input: list[str] = Field(min_length=1, max_length=128)
    model: str | None = None
    workflow: str | None = None


class EmbedResponse(BaseModel):
    embeddings: list[list[float]]
    provider: str
    model: str
    model_served: str | None = None
    tokens_in: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int = Field(description="usage_log.id for this call.")


# ─── Helpers ────────────────────────────────────────────────────────────────


class DecisionRequest(BaseModel):
    # The text to judge. 32k-token model context; the cap here is in chars and
    # generous — the provider rejects past-context input itself.
    state: str = Field(min_length=1, max_length=120_000)
    # {name: {"type": "choice"|"score"|"noul", "instructions": ..., "criteria": ...}}
    # — shapes validated in providers/decisions.validate_questions.
    questions: dict[str, dict[str, Any]] = Field(min_length=1, max_length=64)
    model: str | None = None
    workflow: str | None = None


class DecisionResponse(BaseModel):
    answers: dict[str, Any]
    provider: str
    model: str
    model_served: str | None = None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int = Field(description="usage_log.id for this call.")


def _require_capability_scope(ctx: ProjectCtx, scope: str) -> None:
    if not ctx.has_scope(scope):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"project lacks scope: {scope}")


def _validate_pin(model: str | None, capability: str, chain: list[str] | None = None) -> None:
    """A pinned model must exist in the catalog and serve the capability: a typo
    is a 400 with suggestions HERE, not a job that burns eight retries."""
    if not model:
        return
    try:
        catalog.resolve_pin(model, capability, chain if chain is not None else chain_for(capability))
    except UnknownModel as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


class ModelsResponse(BaseModel):
    object: str = "list"
    data: list[dict[str, Any]]


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
    _validate_pin(body.model, "embedding", [provider])
    await admit_request(ctx.project)

    try:
        outcome = await run_embed(
            project=ctx.project, provider=provider,
            inputs=body.input, model=body.model, workflow=body.workflow,
        )
    except EmbedRequestInvalid as e:
        raise HTTPException(400, str(e)) from e
    except EmbedFailed as e:
        raise HTTPException(502, f"embed failed: {e}") from e
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"no embedding key available for provider={provider}",
        )
    return EmbedResponse(
        embeddings=outcome.embeddings, provider=outcome.provider, model=outcome.model,
        model_served=outcome.model_served,
        tokens_in=outcome.tokens_in, cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms, key_label=outcome.key_label,
        request_id=outcome.request_id,
    )


@router.post("/decisions", response_model=DecisionResponse)
async def decisions_endpoint(
    body: DecisionRequest,
    ctx: ProjectCtx = Depends(require_project),
) -> DecisionResponse:
    """Typed decisions (choice / score / yes-no) — synchronous, like /v1/embed.

    Sync on purpose: a decision answers in ~0.4s (p90 0.46s measured on 120
    real calls), so there is no held-connection problem that /v1/jobs exists
    to solve for multi-second chat completions."""
    _require_capability_scope(ctx, scope_for("decision"))
    try:
        validate_questions(body.questions)
    except DecisionRequestInvalid as e:
        raise HTTPException(422, str(e)) from e
    _validate_pin(body.model, "decision", ["openrouter"])
    await admit_request(ctx.project)
    try:
        outcome = await run_decision(
            project=ctx.project, state=body.state, questions=body.questions,
            model=body.model, workflow=body.workflow,
        )
    except DecisionFailed as e:
        raise HTTPException(502, f"decision failed: {e}") from e
    if outcome is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no paid openrouter key carries llm:decision",
        )
    return DecisionResponse(
        answers=outcome.answers, provider=outcome.provider, model=outcome.model,
        model_served=outcome.model_served, tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out, cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms, key_label=outcome.key_label,
        request_id=outcome.request_id,
    )


class TranscribeResponse(BaseModel):
    text: str
    provider: str
    model: str
    model_served: str | None = None
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int = Field(description="usage_log.id for this call.")


# 25 MB — Whisper's hard limit at both Groq and OpenAI.
_MAX_AUDIO_BYTES = 25 * 1024 * 1024


_AUDIO_READ_CHUNK = 1024 * 1024


async def _read_audio_upload(file: UploadFile) -> bytes:
    """Shared validation for both transcription entry points.

    Reads in chunks and stops at the limit: `await file.read()` pulled the whole
    upload into memory BEFORE the size check, so an oversized body cost its full
    size in RAM per request (2026-10-03 review). The multipart part is already
    spooled by Starlette, so the declared `file.size` is checked first and a
    chunked read bounds memory for anything that under-reports it."""
    too_big = HTTPException(413, f"audio exceeds {_MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    if file.size is not None and file.size > _MAX_AUDIO_BYTES:
        raise too_big
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_AUDIO_READ_CHUNK):
        total += len(chunk)
        if total > _MAX_AUDIO_BYTES:
            raise too_big
        chunks.append(chunk)
    if not total:
        raise HTTPException(400, "empty audio file")
    return b"".join(chunks)


@router.post("/transcribe", response_model=TranscribeResponse)
async def transcribe_endpoint(
    file: UploadFile = File(...),
    workflow: str | None = Query(None),
    ctx: ProjectCtx = Depends(require_project),
) -> TranscribeResponse:
    """Audio → text, SYNCHRONOUS. Multipart `file`. Chain: groq → local →
    gemini → openai. Prefer POST /v1/transcribe/jobs when the slow local
    fallback might serve — see that route."""
    _require_capability_scope(ctx, scope_for("transcription"))
    audio = await _read_audio_upload(file)
    await admit_request(ctx.project)

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
        model_served=outcome.model_served,
        cost_usd=outcome.cost_usd, latency_ms=outcome.latency_ms,
        key_label=outcome.key_label, request_id=outcome.request_id,
    )


# ─── chat:deep — async job API (see services/deep_jobs.py for why) ─────────


class DeepRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1)
    model: str | None = Field(None, description="override provider's default model")
    # Same cost-guard-reservation rationale as ChatRequest (2026-07-16); the
    # deep lane legitimately generates long answers, so its ceiling is higher.
    max_tokens: int = Field(4096, ge=1, le=32768)
    temperature: float = Field(0.7, ge=0.0, le=2.0)
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
    model_served: str | None = Field(
        None,
        description="the EXACT model that answered, when it says more than "
                    "`model` (the routing name) does — e.g. DeepSeek-V4.1-Flash "
                    "behind deepseek/deepseek-flash, or the loaded gguf behind "
                    "local/qwen3vl. Null when the routing name is already the "
                    "exact model id.",
    )
    tokens_in: int | None = None
    tokens_out: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    key_label: str | None = None
    request_id: int | None = None
    error: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    refusal: str | None = None
    # Vision only, and only when the self-hosted `local` provider answered:
    # what the image was, and what shape `text` is in. Additive on purpose —
    # `text` stays prose for every provider, so a client that only reads `text`
    # sees no difference whether local or gemini served the call.
    vision_type: str | None = Field(
        None, description="vision: detected image kind (чек, переписка, …)")
    vision_format: str | None = Field(
        None, description="vision: shape of `text` — text | markdown | json")
    poll_after_s: int | None = Field(
        None, description="present only while status=pending"
    )


@router.post("/deep", response_model=DeepSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def deep_submit(
    body: DeepRequest,
    response: Response,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepSubmitResponse:
    """Submit a chat:deep (long-context/reasoning, 1M-token nemotron) request.
    Returns immediately with a job_id — poll GET /v1/deep/{job_id} for the
    result. Real latency has been observed up to ~8 minutes."""
    _require_capability_scope(ctx, scope_for("chat:deep"))
    _validate_pin(body.model, "chat:deep")
    await admit_request(ctx.project)
    # submit_deep_job needs a real autoincrementing BIGSERIAL id — SQLite
    # doesn't do that for BigInteger, so this whole path is exercised only by
    # the Postgres-only test_deep_submit_creates_job_and_runs_in_background.
    job_id = await submit_deep_job(  # pragma: no cover
        project=ctx.project,
        messages=[m.model_dump() for m in body.messages],
        model=body.model, max_tokens=body.max_tokens, temperature=body.temperature,
        workflow=body.workflow,
    )
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)  # pragma: no cover
    return DeepSubmitResponse(  # pragma: no cover
        job_id=job_id, poll_url=f"/v1/deep/{job_id}", poll_after_s=5,
    )


def _job_response(row: Any) -> DeepJobResponse:
    """Shape a deep_jobs row into the poll response — shared by /v1/deep and
    /v1/jobs.

    `done` is the ONLY terminal-success status. Everything not-yet-terminal —
    `pending` AND `running` (the dispatcher claims a job as `running` while it
    executes, a state the old fire-and-forget path never had) — must map to a
    `pending` response so the client keeps polling; falling through to `done`
    would hand back status=done with text=null and the client would stop
    polling on an empty answer. Any unexpected status defaults to pending for
    the same fail-safe reason."""
    if row.status == "error":
        return DeepJobResponse(job_id=row.id, status="error", error=row.error_message)
    if row.status != "done":
        return DeepJobResponse(
            job_id=row.id, status="pending",
            poll_after_s=next_poll_after_s(row.created_at),
        )
    meta = row.result_meta or {}
    return DeepJobResponse(
        job_id=row.id, status="done", text=row.result_text,
        provider=meta.get("provider"), model=meta.get("model"),
        model_served=meta.get("model_served"),
        tokens_in=meta.get("tokens_in"), tokens_out=meta.get("tokens_out"),
        cache_read_tokens=meta.get("cache_read_tokens"),
        cost_usd=meta.get("cost_usd"), latency_ms=meta.get("latency_ms"),
        key_label=meta.get("key_label"), request_id=meta.get("request_id"),
        vision_type=meta.get("vision_type"),
        vision_format=meta.get("vision_format"),
        tool_calls=meta.get("tool_calls"), finish_reason=meta.get("finish_reason"),
        refusal=meta.get("refusal"),
    )


@router.get("/deep/{job_id}", response_model=DeepJobResponse)
async def deep_poll(
    job_id: int,
    response: Response,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepJobResponse:
    row = await get_job(job_id, ctx.project.id)
    if row is None:
        raise HTTPException(404, "job not found")
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)  # pragma: no cover
    return _job_response(row)  # pragma: no cover


# ─── Generic async jobs — submit+poll for ANY chat capability (Phase 4) ─────
#
# Same submit/poll shape as /v1/deep, opened to every chat capability: the
# caller gets a guaranteed answer (exhaustive rotation, no held connection that
# a slow provider could 504). Sync /v1/chat no longer exists (410 above). See
# docs/routing.md and services/deep_jobs.py.


class JobSubmitResponse(BaseModel):
    job_id: int
    status: str = "pending"
    poll_url: str
    poll_after_s: int = Field(description="suggested wait before the first poll")


@router.post("/jobs", response_model=JobSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def jobs_submit(
    body: ChatRequest,
    response: Response,
    capability: str = Query("chat:fast"),
    ctx: ProjectCtx = Depends(require_project),
) -> JobSubmitResponse:
    """Submit any chat `capability` as an async job. Returns a job_id
    immediately — poll GET /v1/jobs/{job_id}. Takes the same body shape as the
    removed sync chat (incl. response_format), but never holds the connection."""
    if capability not in _JOB_CAPABILITIES:
        raise HTTPException(
            400,
            f"capability={capability} is not available as an async job "
            f"(sync-only or unknown). Async job capabilities: "
            f"{sorted(_JOB_CAPABILITIES)}",
        )
    _require_capability_scope(ctx, scope_for(capability))  # type: ignore[arg-type]
    _validate_pin(body.model, capability)
    messages = [m.model_dump(exclude_unset=True) for m in body.messages]
    if capability == "vision" and (problem := inline_image_problem(messages)):
        # A file no provider can decode must fail HERE, permanently — not walk
        # the chain, retry eight times and be resubmitted by the client (see
        # services/vision_payload.py for the 8-passes-in-24h case behind this).
        raise HTTPException(400, problem)
    await admit_request(ctx.project)
    job_id = await submit_job(  # pragma: no cover
        project=ctx.project, capability=capability,
        messages=messages,
        model=body.model, max_tokens=body.max_tokens, temperature=body.temperature,
        response_format=body.response_format, workflow=body.workflow,
        extra=({"tools": [t.model_dump(exclude_none=True) for t in body.tools],
                "tool_choice": body.tool_choice} if body.tools else None),
    )
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)  # pragma: no cover
    return JobSubmitResponse(  # pragma: no cover
        job_id=job_id, poll_url=f"/v1/jobs/{job_id}", poll_after_s=2,
    )


@router.post("/transcribe/jobs", response_model=JobSubmitResponse,
             status_code=status.HTTP_202_ACCEPTED)
async def transcribe_submit(
    response: Response,
    file: UploadFile = File(...),
    workflow: str | None = Query(None),
    ctx: ProjectCtx = Depends(require_project),
) -> JobSubmitResponse:
    """Audio → text, ASYNC: returns a job_id immediately, poll GET /v1/jobs/{id}.

    Same chain and result as POST /v1/transcribe; the difference is who waits.
    The chain's fallback (self-hosted faster-whisper) legitimately takes
    131-168s on this host, which is past any sane client read timeout — so a
    synchronous call simply LOST those transcripts when groq's daily quota was
    spent. Queued, that slow path gets to finish and the caller polls for it,
    with the queue's retries/backpressure/restart-survival on top.

    The sync endpoint stays for the fast path (groq, ~750ms) and for callers
    that prefer one round-trip."""
    _require_capability_scope(ctx, scope_for("transcription"))
    audio = await _read_audio_upload(file)
    await admit_request(ctx.project)
    job_id = await submit_job(  # pragma: no cover — needs the real queue (Postgres)
        project=ctx.project, capability="transcription",
        messages=[], model=None, max_tokens=0, temperature=0.0,
        response_format=None, workflow=workflow,
        extra={AUDIO_FIELD: base64.b64encode(audio).decode(),
               "filename": file.filename or "audio.ogg"},
    )
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)  # pragma: no cover — same
    return JobSubmitResponse(  # pragma: no cover — same
        job_id=job_id, poll_url=f"/v1/jobs/{job_id}", poll_after_s=2,
    )


@router.get("/jobs/{job_id}", response_model=DeepJobResponse)
async def jobs_poll(
    job_id: int,
    response: Response,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepJobResponse:
    row = await get_job(job_id, ctx.project.id)
    if row is None:
        raise HTTPException(404, "job not found")
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)  # pragma: no cover
    return _job_response(row)  # pragma: no cover


# ─── Model catalog ──────────────────────────────────────────────────────────


@router.get("/models", response_model=ModelsResponse)
async def list_models(ctx: ProjectCtx = Depends(require_project)) -> ModelsResponse:
    """Every model a caller can pin via `model` (any authenticated project key).

    Built from the provider registry — see providers/catalog.py. Pin either an
    `id` (`gemini/gemini-2.5-flash`) or a bare `name` (`gpt-oss-120b`); a name
    served by several providers resolves deterministically (free-tier keys first,
    then fixed provider rank)."""
    return ModelsResponse(data=catalog.listing())
