"""LLM proxy mode — broker calls the provider with its key, returns the response.

Endpoint: POST /v1/jobs?capability=chat:fast (async chat; POST /v1/chat is a 410)
Endpoint: POST /v1/embed?provider=voyage

Thin layer: authenticate, gate on the capability's scope, delegate to
services.llm_service, shape the response. All orchestration lives in the service.
"""
from __future__ import annotations

import base64
import logging
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from aibroker.agent.accounts import pick_account
from aibroker.agent.client_tools import (
    MAX_RESULT_BYTES,
    ClientToolConflict,
    is_waiting,
    submit_result,
)
from aibroker.agent.constants import AGENT_SCOPE, AgentProjectNotAllowed, project_allowed
from aibroker.agent.policy import AgentPolicyViolation, policy_for, policy_violation
from aibroker.agent.rpc import wake_worker
from aibroker.agent.runs import RunRejected, create_run, served_model_meta
from aibroker.agent.subscription_input import subscription_messages
from aibroker.agent.subscription_job import (
    json_schema_problem,
    live_files,
    prompt_for_messages,
)
from aibroker.auth import ProjectCtx, require_project
from aibroker.config import get_settings
from aibroker.providers import catalog
from aibroker.providers.catalog import UnknownModel
from aibroker.providers.decisions import DecisionRequestInvalid, validate_questions
from aibroker.providers.job_contract import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_TEMPERATURE,
    JOB_CAPABILITIES,
    JOB_MAX_TOKENS_MAX,
    JOB_MAX_TOKENS_MIN,
    JOB_TEMPERATURE_MAX,
    JOB_TEMPERATURE_MIN,
    subscription_prompt_problem,
    subscription_request_problem,
)
from aibroker.providers.subscriptions import (
    PROVIDERS,
    subscription_model,
    subscription_model_verified,
    subscription_tool_model_canary,
    subscription_tools_enabled,
)
from aibroker.routes.jobs_control import idempotency_conflict, known_job_id
from aibroker.routing import chain_for, scope_for
from aibroker.services.deep_jobs import (
    AUDIO_FIELD,
    IdempotencyConflict,
    build_request,
    get_job,
    next_poll_after_s,
    payload_hash,
    submit_deep_job,
    submit_job,
    submit_job_ex,
)
from aibroker.services.job_meta import check_labels, check_token, resolve_trace_id
from aibroker.services.llm_service import (
    DecisionFailed,
    EmbedFailed,
    EmbedRequestInvalid,
    TranscribeFailed,
    run_decision,
    run_embed,
    run_transcribe,
)
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
_JOB_CAPABILITIES = JOB_CAPABILITIES

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
    max_tokens: int = Field(DEFAULT_MAX_TOKENS, ge=JOB_MAX_TOKENS_MIN, le=JOB_MAX_TOKENS_MAX)
    temperature: float = Field(DEFAULT_TEMPERATURE, ge=JOB_TEMPERATURE_MIN,
                               le=JOB_TEMPERATURE_MAX)
    response_format: dict[str, Any] | None = None
    workflow: str | None = None
    tools: list[ToolDefinition] | None = Field(None, min_length=1, max_length=32)
    tool_choice: str | dict[str, Any] | None = None
    # Subscription client tools only: output tools whose caller result may end the job
    # (POST /tool-results with `terminal: true`) instead of starting another model turn.
    terminal_tools: list[str] | None = Field(None, min_length=1, max_length=32)
    stream: bool | None = None
    # Client correlation (docs/api.md): stored on the job, echoed on submit and every poll, never
    # part of the payload hash. `trace_id` may also arrive as the X-Trace-Id header (body wins).
    trace_id: str | None = None
    labels: dict[str, str] | None = None

    @field_validator("trace_id")
    @classmethod
    def valid_trace_id(_cls, value: str | None) -> str | None:
        return None if value is None else check_token(value, "trace_id")

    @field_validator("labels")
    @classmethod
    def valid_labels(_cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return check_labels(value)

    @model_validator(mode="after")
    def valid_tools(self) -> ChatRequest:
        if self.tools:
            if subscription_model(self.model) is None:
                tool_model_provider(self.model)
            if self.response_format:
                raise ValueError("tools and response_format are mutually exclusive")
            validate_choice([tool.model_dump(exclude_none=True) for tool in self.tools],
                            self.tool_choice)
        elif self.tool_choice is not None:
            raise ValueError("tool_choice requires tools")
        if self.terminal_tools is not None:
            names = {tool.function.name for tool in self.tools or []}
            if (not self.tools or subscription_model(self.model) is None
                    or len(set(self.terminal_tools)) != len(self.terminal_tools)
                    or not set(self.terminal_tools) <= names):
                raise ValueError("terminal_tools must name declared tools of a subscription job")
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


class ModelObservation(BaseModel):
    model: str
    kind: str = Field(description="response|thread_config|session_init")
    source: str


class JobEvidence(BaseModel):
    """Subscription jobs only: what served the job and how well that is evidenced."""
    provider: str | None = None
    account_id: str | None = None
    billing_mode: str | None = Field(None, description="from the run's billing type")
    model_requested: str | None = Field(None, description="the requested pin")
    model_served: str | None = Field(
        None, description="the best observed model, labelled by model_served_kind (it may be "
                          "only a thread_config or session_init value); null when none was "
                          "observed - never the requested model. The top-level poll "
                          "`model_served` is stricter: response-level evidence only")
    model_served_kind: str = Field(
        "unknown", description="response (API response message) | thread_config (the CLI's "
        "thread configuration, not proof of each response's backend) | session_init | unknown")
    model_served_source: str = "none"
    model_observations: list[ModelObservation] = Field(default_factory=list)
    run_status: str | None = Field(
        None, description="pending|running while in flight; completed|failed|cancelled|timed_out "
                          "once terminal")
    observations_truncated: bool = Field(
        False, description="an observation was dropped or evicted at the cap of 10")
    observations_complete: bool = Field(
        True, description="false when observations_truncated: the summary may be incomplete")
    model_rerouted: bool | None = Field(
        None, description="false ONLY for a completed Codex app-server run (reroute notifications "
                          "are observed there and fail the run closed); null when unknown "
                          "(Claude, Codex exec, nothing observed, or a run that did not complete)")


class DeepJobResponse(BaseModel):
    job_id: int
    status: str = Field(description="pending|done|error|cancelled")
    trace_id: str | None = Field(None, description="client trace id given at submit, if any")
    labels: dict[str, str] | None = Field(None, description="client labels given at submit, if any")
    cancel_requested: bool | None = Field(
        None, description="true while a cancel was requested but the job is not terminal yet")
    text: str | None = None
    provider: str | None = None
    model: str | None = None
    requested_model: str | None = Field(None, description="requested model pin, if provided")
    model_served: str | None = Field(
        None,
        description="independently reported served model, when available. For subscription "
                    "runs only response-level evidence fills it (an API response message); "
                    "thread_config / session_init / nothing leave it null (see `evidence`); "
                    "never inferred from the requested pin",
    )
    tokens_in: int | None = None
    tokens_out: int | None = None
    total_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    cost_usd: float | None = None
    billing_type: str | None = None
    account_id: str | None = Field(
        None, description="subscription jobs: which login (account) served the run")
    latency_ms: int | None = None
    key_label: str | None = None
    request_id: int | None = None
    evidence: JobEvidence | None = Field(
        None, description="subscription jobs only: served-model evidence (see docs/api.md)")
    error: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    refusal: str | None = None
    files: list[dict[str, Any]] | None = None
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


def _require_image_access(ctx: ProjectCtx, messages: list[dict[str, Any]], *,
                          check_decode: bool = True) -> None:
    """image_url blocks are gated by content, not capability name: any job carrying
    one reaches a vision-capable model, so it needs llm:vision (403, checked first)
    and, unless `check_decode` is off, an inline image some provider can decode (400)."""
    if not any(isinstance(m.get("content"), list) and any(
        isinstance(b, dict) and b.get("type") == "image_url" for b in m["content"])
        for m in messages):
        return
    _require_capability_scope(ctx, scope_for("vision"))
    if check_decode and (problem := inline_image_problem(messages)):
        # A file no provider can decode must fail HERE, permanently — not walk
        # the chain, retry eight times and be resubmitted by the client (see
        # services/vision_payload.py for the 8-passes-in-24h case behind this).
        raise HTTPException(400, problem)


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
    messages = [m.model_dump() for m in body.messages]
    _require_image_access(ctx, messages)
    _validate_pin(body.model, "chat:deep")
    await admit_request(ctx.project)
    # submit_deep_job needs a real autoincrementing BIGSERIAL id — SQLite
    # doesn't do that for BigInteger, so this whole path is exercised only by
    # the Postgres-only test_deep_submit_creates_job_and_runs_in_background.
    job_id = await submit_deep_job(  # pragma: no cover
        project=ctx.project,
        messages=messages,
        model=body.model, max_tokens=body.max_tokens, temperature=body.temperature,
        workflow=body.workflow,
    )
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)  # pragma: no cover
    return DeepSubmitResponse(  # pragma: no cover
        job_id=job_id, poll_url=f"/v1/deep/{job_id}", poll_after_s=5,
    )


def _job_evidence(meta: dict[str, Any], request: dict[str, Any] | None = None,
                  run_status: str | None = None) -> JobEvidence | None:
    """Evidence envelope of a subscription (client_job) run, for any status; None for a plain job.
    Derived from the stored observations, so it is also available while the run is in flight."""
    cj = (request or {}).get("client_job")
    if not cj and not (meta.get("account_id") and meta.get("provider")
                       and "model_served_kind" in meta):
        return None
    cj = cj or {}
    truncated = bool(meta.get("observations_truncated"))
    served = served_model_meta(meta.get("model_observations"), truncated)
    outcome = meta.get("run_outcome")
    return JobEvidence(
        provider=meta.get("provider") or cj.get("provider"),
        account_id=meta.get("account_id") or (request or {}).get("account_id"),
        billing_mode=meta.get("billing_type"),
        model_requested=meta.get("model") or cj.get("model"),
        model_served=served["model_observed_best"],
        model_served_kind=served["model_served_kind"],
        model_served_source=served["model_served_source"],
        model_observations=[ModelObservation(**o) for o in served["model_observations"]],
        run_status=outcome or run_status,
        observations_truncated=truncated, observations_complete=not truncated,
        model_rerouted=False if outcome == "completed" and meta.get("app_server_path") else None,
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
    request = getattr(row, "request", None) or {}
    requested_model = (request.get("client_job") or {}).get("model") or request.get("model")
    # Subscription runs only: the account is chosen at creation and never changes.
    account_id = request.get("account_id") if request.get("client_job") else None
    tags: dict[str, Any] = {"trace_id": getattr(row, "trace_id", None),
                            "labels": getattr(row, "labels", None)}
    if row.status == "error":
        return DeepJobResponse(job_id=row.id, status="error", error=row.error_message,
                               requested_model=requested_model, account_id=account_id,
                               evidence=_job_evidence(row.result_meta or {}, request, "failed"),
                               **tags)
    if row.status == "cancelled":
        return DeepJobResponse(job_id=row.id, status="cancelled",
                               requested_model=requested_model, account_id=account_id,
                               evidence=_job_evidence(row.result_meta or {}, request, "cancelled"),
                               **tags)
    if row.status != "done":
        pending_tool = (row.result_meta or {}).get("client_tool") or {}
        call = None
        if is_waiting(pending_tool):
            call = {"id": pending_tool["call_id"], "type": "function",
                    "function": {"name": pending_tool["name"],
                                 "arguments": pending_tool["arguments"]}}
        return DeepJobResponse(
            job_id=row.id, status="pending",
            requested_model=requested_model, account_id=account_id,
            evidence=_job_evidence(row.result_meta or {}, request, str(row.status)),
            cancel_requested=True if getattr(row, "cancel_requested", False) else None, **tags,
            poll_after_s=next_poll_after_s(row.created_at) if call is None else None,
            tool_calls=[call] if call else None,
            finish_reason="tool_calls" if call else None,
        )
    meta = row.result_meta or {}
    return DeepJobResponse(
        job_id=row.id, status="done", text=row.result_text,
        provider=meta.get("provider"), model=meta.get("model"),
        requested_model=requested_model,
        model_served=meta.get("model_served"),
        evidence=_job_evidence(meta, request, "completed"),
        tokens_in=meta.get("tokens_in"), tokens_out=meta.get("tokens_out"),
        total_tokens=meta.get("total_tokens"),
        cache_read_tokens=meta.get("cache_read_tokens"),
        account_id=meta.get("account_id") or account_id,
        cache_write_tokens=meta.get("cache_write_tokens"),
        reasoning_tokens=meta.get("reasoning_tokens"),
        cost_usd=meta.get("cost_usd"), latency_ms=meta.get("latency_ms"),
        billing_type=meta.get("billing_type"),
        key_label=meta.get("key_label"), request_id=meta.get("request_id"),
        vision_type=meta.get("vision_type"),
        vision_format=meta.get("vision_format"),
        tool_calls=meta.get("tool_calls"), finish_reason=meta.get("finish_reason"),
        refusal=meta.get("refusal"),
        files=live_files(meta.get("files")),
        **tags,
    )


def _require_agent_read_access(ctx: ProjectCtx, row: Any) -> None:
    """Re-authorize reading an agent/subscription job. The gate runs at read time,
    not only at submit, so revoking llm:agent or dropping the project from
    AGENT_ALLOWED_PROJECT_IDS also closes already-created results on EVERY poll route."""
    if not row.capability.startswith("agent:"):
        return
    if not ctx.has_scope(AGENT_SCOPE):
        raise HTTPException(403, f"project lacks scope: {AGENT_SCOPE}")
    if not project_allowed(ctx.project.id):
        raise AgentProjectNotAllowed(ctx.project.id)
    violation = policy_violation(ctx.project.id, row.request.get("model"),
                                 row.request.get("account_id", ""), row.capability)
    if violation:
        raise AgentPolicyViolation(violation)


@router.get("/deep/{job_id}", response_model=DeepJobResponse)
async def deep_poll(
    job_id: int,
    response: Response,
    ctx: ProjectCtx = Depends(require_project),
) -> DeepJobResponse:
    row = await get_job(job_id, ctx.project.id)
    if row is None:
        raise HTTPException(404, "job not found")
    _require_agent_read_access(ctx, row)
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
    trace_id: str | None = None
    labels: dict[str, str] | None = None


def _client_request(body: ChatRequest) -> dict[str, Any]:
    """The client payload as the plain path stores it (== the plain payload_hash input). Subscription
    jobs hash THIS too for an Idempotency-Key, so the broker-picked account never causes a 409."""
    return build_request(
        messages=[m.model_dump(exclude_unset=True) for m in body.messages], model=body.model,
        max_tokens=body.max_tokens, temperature=body.temperature,
        response_format=body.response_format, workflow=body.workflow,
        extra=({"tools": [t.model_dump(exclude_none=True) for t in body.tools],
                "tool_choice": body.tool_choice,
                # only when set, so payload hashes of older clients do not change
                **({"terminal_tools": body.terminal_tools} if body.terminal_tools else {})}
               if body.tools else None))


async def _replay(ctx: ProjectCtx, job_id: int) -> JSONResponse:
    """200 + the job's CURRENT poll body for an Idempotency-Key replay (never a new job)."""
    row = await get_job(job_id, ctx.project.id)
    if row is None:  # pragma: no cover — pruned between the key lookup and this read
        raise HTTPException(404, "job not found")
    _require_agent_read_access(ctx, row)
    content = {**jsonable_encoder(_job_response(row)), "poll_url": f"/v1/jobs/{job_id}"}
    return JSONResponse(content, headers={
        "Idempotent-Replay": "true", REQUEST_ID_HEADER: job_request_id(job_id)})


@router.post("/jobs", response_model=JobSubmitResponse, status_code=status.HTTP_202_ACCEPTED,
             responses={200: {"model": DeepJobResponse,
                              "description": "Idempotent-Replay: same Idempotency-Key + payload"},
                        409: {"description": "idempotency_key_conflict: key used with another payload"}})
async def jobs_submit(
    body: ChatRequest,
    response: Response,
    capability: str = Query("chat:fast"),
    idempotency_key: Annotated[str | None, Header(
        description="<=128 chars [A-Za-z0-9._:-]; same key + payload returns the same job")] = None,
    x_trace_id: Annotated[str | None, Header(description="alternative to body `trace_id`")] = None,
    ctx: ProjectCtx = Depends(require_project),
) -> JobSubmitResponse | JSONResponse:
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
    try:
        trace_id = resolve_trace_id(body.trace_id, x_trace_id)
        if idempotency_key is not None:
            check_token(idempotency_key, "Idempotency-Key")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    client_hash = (payload_hash(ctx.project.id, capability, _client_request(body))
                   if idempotency_key else None)
    subscription = subscription_model(body.model)
    # Subscription jobs validate their own attachments in subscription_messages.
    _require_image_access(ctx, [m.model_dump(exclude_unset=True) for m in body.messages],
                          check_decode=subscription is None)
    if subscription is not None:
        if not ctx.has_scope(AGENT_SCOPE):
            raise HTTPException(403, f"project lacks scope: {AGENT_SCOPE}")
        if not project_allowed(ctx.project.id):
            raise AgentProjectNotAllowed(ctx.project.id)
        # A retry of a known key must not meet config checks that changed since the first submit.
        if (known := await known_job_id(ctx.project.id, idempotency_key, client_hash)) is not None:
            return await _replay(ctx, known)
        _validate_pin(body.model, capability, [*chain_for(capability), subscription.provider])
        if subscription.provider == "codex-sub" and not get_settings().AGENT_SUBSCRIPTION_CODEX_ENABLED:
            raise HTTPException(503, "codex subscription route is not enabled")
        if not subscription_model_verified(subscription.id):
            raise HTTPException(503, "subscription model availability is not verified")
        cli_model = subscription.id.split("/", 1)[1]
        account_id = "owner"      # Codex has a single login; only Claude rotates accounts
        policy = policy_for(ctx.project.id)
        if policy is not None and subscription.provider == "claude-sub":
            account_id = await pick_account(
                policy.ordered_accounts(), PROVIDERS[subscription.provider]) or account_id
        violation = policy_violation(ctx.project.id, cli_model, account_id,
                                     PROVIDERS[subscription.provider])
        if violation:
            raise AgentPolicyViolation(violation)
        if problem := subscription_request_problem(body, capability, subscription.provider):
            raise HTTPException(400, problem)
        if body.tools and not (subscription_tools_enabled(subscription.provider)
                               and (catalog.supports_tools(subscription)
                                    or subscription_tool_model_canary(subscription.id,
                                                                      ctx.project.id))):
            raise HTTPException(503, "subscription client tools are not enabled for this project and model")
        messages = [m.model_dump(exclude_unset=True) for m in body.messages]
        try:
            messages, attachments = subscription_messages(messages)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if attachments and "vision" not in subscription.capabilities:
            raise HTTPException(503, "subscription model input vision is not verified")
        prompt = prompt_for_messages(messages, body.response_format)
        if problem := subscription_prompt_problem(prompt):
            raise HTTPException(400, problem)
        await admit_request(ctx.project)
        try:
            job_id, created = await create_run(
                project=ctx.project, capability=PROVIDERS[subscription.provider], prompt=prompt,
                task_id="jobs", account_id=account_id, session_id=None, idempotency_key=None,
                wall_timeout_s=1800, idle_timeout_s=300, allowed_tools=[], model=cli_model,
                attachments=attachments,
                client_job={"capability": capability, "provider": subscription.provider,
                            "model": subscription.id, "response_format": body.response_format,
                            "workflow": body.workflow,
                            "tools": [t.model_dump(exclude_none=True) for t in body.tools or []],
                            "tool_choice": body.tool_choice,
                            "terminal_tools": body.terminal_tools or []},
                job_idempotency_key=idempotency_key, job_payload_hash=client_hash,
                trace_id=trace_id, labels=body.labels,
            )
        except RunRejected as exc:
            raise HTTPException(400, str(exc)) from exc
        except IdempotencyConflict as exc:   # lost the key race to a different payload
            raise idempotency_conflict(exc) from exc
        if idempotency_key and not created:
            return await _replay(ctx, job_id)
        response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)
        await wake_worker(PROVIDERS[subscription.provider], job_id)
        return JobSubmitResponse(job_id=job_id, poll_url=f"/v1/jobs/{job_id}", poll_after_s=2,
                                 trace_id=trace_id, labels=body.labels)
    if (known := await known_job_id(ctx.project.id, idempotency_key, client_hash)) is not None:
        return await _replay(ctx, known)
    _validate_pin(body.model, capability)
    if (body.response_format and body.response_format.get("type") == "json_schema"
            and (problem := json_schema_problem(body.response_format))):   # never parse-only
        raise HTTPException(400, problem)
    messages = [m.model_dump(exclude_unset=True) for m in body.messages]
    await admit_request(ctx.project)
    try:
        outcome = await submit_job_ex(
            project=ctx.project, capability=capability,
            messages=messages,
            model=body.model, max_tokens=body.max_tokens, temperature=body.temperature,
            response_format=body.response_format, workflow=body.workflow,
            extra=({"tools": [t.model_dump(exclude_none=True) for t in body.tools],
                    "tool_choice": body.tool_choice} if body.tools else None),
            trace_id=trace_id, labels=body.labels, idempotency_key=idempotency_key,
        )
    except IdempotencyConflict as exc:   # lost the key race to a different payload
        raise idempotency_conflict(exc) from exc
    if outcome.replayed:
        return await _replay(ctx, outcome.job_id)
    job_id = outcome.job_id
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)
    return JobSubmitResponse(
        job_id=job_id, poll_url=f"/v1/jobs/{job_id}", poll_after_s=2,
        trace_id=trace_id, labels=body.labels,
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
    A slow provider in the chain (gemini/openai on a long clip) can take
    tens of seconds, which is past some client read timeouts, so a
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
    _require_agent_read_access(ctx, row)
    response.headers[REQUEST_ID_HEADER] = job_request_id(job_id)  # pragma: no cover
    return _job_response(row)  # pragma: no cover


# ─── Model catalog ──────────────────────────────────────────────────────────


class ClientToolResultIn(BaseModel):
    call_id: str = Field(min_length=1, max_length=128)
    success: bool
    content: str = Field(max_length=MAX_RESULT_BYTES)
    # true: this is the result of a declared terminal output tool; the worker acknowledges it,
    # stops the model turn and finishes the job with empty text instead of another turn
    terminal: bool = False


@router.post("/jobs/{job_id}/tool-results", status_code=status.HTTP_202_ACCEPTED)
async def jobs_tool_result(
    job_id: int, body: ClientToolResultIn,
    ctx: ProjectCtx = Depends(require_project),
) -> dict[str, bool]:
    """Accept one caller-executed result for a pending tool on that caller's job."""
    _require_capability_scope(ctx, AGENT_SCOPE)
    if not project_allowed(ctx.project.id):
        raise AgentProjectNotAllowed(ctx.project.id)
    row = await get_job(job_id, ctx.project.id)
    if row is None:
        raise HTTPException(404, "job not found")
    violation = policy_violation(ctx.project.id, row.request.get("model"),
                                 row.request.get("account_id", ""), row.capability)
    if violation:
        raise AgentPolicyViolation(violation)
    try:
        accepted = await submit_result(job_id, ctx.project.id, body.call_id,
                                       body.success, body.content, terminal=body.terminal)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(413, str(exc)) from exc
    except ClientToolConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": accepted}


@router.get("/models", response_model=ModelsResponse)
async def list_models(ctx: ProjectCtx = Depends(require_project)) -> ModelsResponse:
    """Every model a caller can pin via `model` (any authenticated project key).

    Built from the provider registry — see providers/catalog.py. Pin either an
    `id` (`gemini/gemini-2.5-flash`) or a bare `name` (`gpt-oss-120b`); a name
    served by several providers resolves deterministically (free-tier keys first,
    then fixed provider rank)."""
    return ModelsResponse(data=catalog.listing())
