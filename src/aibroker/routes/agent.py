"""Agent runs API (owner-only, scope llm:agent): /v1/agent/runs. See docs/agent-runs.md."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from aibroker.agent.attachments import MAX_FILES, NAME_PATTERN, validate_attachments
from aibroker.agent.codex_cli import UUID_RE
from aibroker.agent.constants import AGENT_SCOPE, AgentProjectNotAllowed, project_allowed
from aibroker.agent.policy import AgentPolicyViolation, policy_violation
from aibroker.agent.rpc import wake_worker
from aibroker.agent.runs import (
    RunConflict,
    RunRejected,
    cancel_run,
    create_run,
    get_attestation,
    get_run,
    list_events,
)
from aibroker.agent.tools import tool_problem
from aibroker.auth import ProjectCtx, require_project
from aibroker.config import get_settings
from aibroker.services.request_cap import admit_request
from aibroker.telemetry.request_context import REQUEST_ID_HEADER, job_request_id

router = APIRouter(tags=["agent"], prefix="/agent")

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"


class AttachmentIn(BaseModel):
    name: str = Field(pattern=NAME_PATTERN)
    media_type: str
    data_b64: str = Field(min_length=1)


class AgentRunRequest(BaseModel):
    capability: Literal["agent:codex", "agent:claude"]
    prompt: str = Field(min_length=1, max_length=200_000)
    task_id: str = Field(min_length=1, max_length=128, pattern=_ID)
    account_id: str = Field(min_length=1, max_length=64, pattern=_ID)
    session_id: str | None = Field(None, min_length=1, max_length=128, pattern=_ID,
                                   description="exact CLI session to resume")
    idempotency_key: str | None = Field(None, min_length=1, max_length=128)
    wall_timeout_s: int = Field(1800, ge=10, le=7200)
    idle_timeout_s: int = Field(300, ge=5, le=1800)
    allowed_tools: list[str] = Field(default_factory=list, max_length=32)
    run_mode: Literal["manual", "autonomous", "exam"] = Field(
        "autonomous", description="autonomous/exam runs are refused until the owner verified "
                                  "paid continuation OFF")
    accept_paid_continuation_risk: bool = Field(
        False, description="manual runs only: accept that extra usage may be enabled")
    deadline_s: int = Field(21600, ge=60, le=604800,
                            description="overall limit incl. quota waits; then run.timed_out")
    attachments: list[AttachmentIn] = Field(default_factory=list, max_length=MAX_FILES,
                                            description="real input files (images)")
    model: str | None = Field(None, min_length=1, max_length=128,
                              pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/@-]*$",
                              description="exact model id; passed through, never substituted")


class AgentRunSubmitResponse(BaseModel):
    run_id: int
    status: str
    deduplicated: bool = False
    poll_url: str


class AgentRunResponse(BaseModel):
    run_id: int
    status: str = Field(
        description="pending|waiting_quota|running|completed|failed|cancelled|timed_out")
    not_before: str | None = Field(None, description="waiting_quota: earliest next attempt (UTC)")
    quota_waits: int = 0
    capability: str
    task_id: str
    account_id: str
    session_id: str | None = None
    cancel_requested: bool = False
    final_text: str | None = None
    files: list[dict[str, Any]] | None = None
    error: str | None = None
    model: str | None = None
    requested_model: str | None = Field(None, description="exact CLI model requested for this run")
    model_served: str | None = Field(
        None, description="actual served model, if independently attested by the CLI; unknown for current subscription runs")
    billing_mode: str | None = None
    usage: dict[str, int | None] | None = None
    usage_source: str | None = None
    auth_method: str | None = Field(None, description="`claude auth status` method of THIS run")
    api_provider: str | None = Field(None, description="`claude auth status` provider of THIS run")
    paid_continuation_verified_off: bool | None = Field(
        None, description="the worker setting at run start")
    fallback: str | None = Field(None, description="`none`: API fallback is refused, recorded per run")
    project_daily_cap_usd: float | None = Field(None, description="the project's cap at run start")
    cost_usd: float | None = Field(
        None, description="NULL unless a cost is confirmed; subscription usage is not priced")


class AgentEventOut(BaseModel):
    seq: int
    ts: str
    type: str
    payload: dict[str, Any]


class AgentEventsResponse(BaseModel):
    run_id: int
    events: list[AgentEventOut]
    next_after_seq: int
    finished: bool


def _require_agent_scope(ctx: ProjectCtx) -> None:
    """Scope first, then the server-side project allowlist (fail closed when it is empty)."""
    if not ctx.has_scope(AGENT_SCOPE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"project lacks scope: {AGENT_SCOPE}")
    if not project_allowed(ctx.project.id):
        raise AgentProjectNotAllowed(ctx.project.id)


def _live_files(files: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Past its TTL a file keeps its name/size but loses its content."""
    if files is None:
        return None
    now = datetime.now(UTC).isoformat()
    out = []
    for f in files:
        if f.get("expires_at") and f["expires_at"] < now:
            f = {**f, "content_b64": None, "expired": True}
        out.append(f)
    return out


def _shape(run: Any, job: Any, attested: dict[str, Any] | None = None) -> AgentRunResponse:
    state = run.outcome or ("running" if job.status == "running" else (
        "waiting_quota" if run.quota_waits else "pending"))
    files = _live_files((job.result_meta or {}).get("files"))
    usage = None
    if run.usage_source is not None:
        observed = (job.result_meta or {}).get("usage_observed") or {}
        usage = {"input_tokens": run.usage_input_tokens, "output_tokens": run.usage_output_tokens,
                 "total_tokens": observed.get("total_tokens"),
                 "cache_read_tokens": run.usage_cache_read_tokens,
                 "reasoning_tokens": observed.get("reasoning_tokens"), "turns": run.usage_turns}
    return AgentRunResponse(
        run_id=run.run_id, status=state, capability=run.capability, task_id=run.task_id,
        account_id=run.account_id, session_id=run.session_id,
        cancel_requested=run.cancel_requested, final_text=job.result_text,
        files=files, error=job.error_message, model=run.model,
        requested_model=run.model, model_served=None,
        quota_waits=run.quota_waits,
        not_before=job.run_after.isoformat() if state == "waiting_quota" and job.run_after else None,
        billing_mode=run.billing_mode, usage=usage, usage_source=run.usage_source,
        cost_usd=run.cost_usd,
        **{k: (attested or {}).get(k) for k in (
            "auth_method", "api_provider", "paid_continuation_verified_off", "fallback",
            "project_daily_cap_usd")},
    )


@router.post("/runs", response_model=AgentRunSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def agent_run_submit(
    body: AgentRunRequest, response: Response, ctx: ProjectCtx = Depends(require_project),
) -> AgentRunSubmitResponse:
    """Queue an agent run. Owner-only: needs the llm:agent scope, the project on the allowlist and a
    policy that lists this exact model and account."""
    _require_agent_scope(ctx)
    violation = policy_violation(ctx.project.id, body.model, body.account_id, body.capability)
    if violation:
        raise AgentPolicyViolation(violation)
    if body.capability == "agent:codex":
        if not get_settings().AGENT_SUBSCRIPTION_CODEX_ENABLED:
            raise HTTPException(503, "codex subscription route is not enabled")
        if body.allowed_tools:
            raise HTTPException(422, "agent:codex is sandboxed to the workspace; allowed_tools must be empty")
        if body.session_id and not UUID_RE.match(body.session_id):
            raise HTTPException(422, "agent:codex resumes only an exact thread id (UUID)")
    for tool in body.allowed_tools if body.capability == "agent:claude" else ():
        problem = tool_problem(tool)
        if problem:
            raise HTTPException(422, problem)
    try:
        validate_attachments([a.model_dump() for a in body.attachments])
    except ValueError as e:
        raise HTTPException(422, str(e)) from e
    try:
        run_id, created = await create_run(
            project=ctx.project, capability=body.capability, prompt=body.prompt,
            task_id=body.task_id, account_id=body.account_id, session_id=body.session_id,
            idempotency_key=body.idempotency_key, wall_timeout_s=body.wall_timeout_s,
            idle_timeout_s=body.idle_timeout_s, allowed_tools=body.allowed_tools,
            model=body.model, deadline_s=body.deadline_s, run_mode=body.run_mode,
            accept_paid_continuation_risk=body.accept_paid_continuation_risk,
            attachments=[a.model_dump() for a in body.attachments],
        )
    except RunRejected as e:
        raise HTTPException(400, str(e)) from e
    except RunConflict as e:
        raise HTTPException(409, str(e)) from e
    if created:
        await admit_request(ctx.project)
    await wake_worker(body.capability, run_id)
    response.headers[REQUEST_ID_HEADER] = job_request_id(run_id)
    return AgentRunSubmitResponse(run_id=run_id, status="pending", deduplicated=not created,
                                  poll_url=f"/v1/agent/runs/{run_id}")


async def _owned_run(run_id: int, ctx: ProjectCtx) -> Any:
    """The caller's run, re-checked against AGENT_PROJECT_POLICY on read: a policy
    removed after the run was queued must close its output, like scope and allowlist."""
    hit = await get_run(run_id, ctx.project.id)
    if hit is None:
        raise HTTPException(404, "run not found")
    run = hit[0]
    violation = policy_violation(ctx.project.id, run.model, run.account_id, run.capability)
    if violation:
        raise AgentPolicyViolation(violation)
    return hit


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
async def agent_run_get(run_id: int, ctx: ProjectCtx = Depends(require_project)) -> AgentRunResponse:
    _require_agent_scope(ctx)
    hit = await _owned_run(run_id, ctx)
    return _shape(*hit, await get_attestation(run_id))


@router.get("/runs/{run_id}/events", response_model=AgentEventsResponse)
async def agent_run_events(
    run_id: int, after_seq: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000),
    ctx: ProjectCtx = Depends(require_project),
) -> AgentEventsResponse:
    """Poll events after `after_seq`; pass `next_after_seq` back until `finished`."""
    _require_agent_scope(ctx)
    hit = await _owned_run(run_id, ctx)
    rows = await list_events(run_id, after_seq, limit)
    last = rows[-1].seq if rows else after_seq
    return AgentEventsResponse(
        run_id=run_id,
        events=[AgentEventOut(seq=r.seq, ts=r.ts.isoformat(), type=r.type, payload=r.payload)
                for r in rows],
        next_after_seq=last, finished=hit[0].outcome is not None and last >= hit[0].last_seq,
    )


@router.post("/runs/{run_id}/cancel", response_model=AgentRunResponse)
async def agent_run_cancel(run_id: int, ctx: ProjectCtx = Depends(require_project)) -> AgentRunResponse:
    _require_agent_scope(ctx)
    hit = await cancel_run(run_id, ctx.project.id)
    if hit is None:
        raise HTTPException(404, "run not found")
    return _shape(*hit, await get_attestation(run_id))
