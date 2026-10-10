"""Agent runs API (owner-only, scope llm:agent): /v1/agent/runs. See docs/agent-runs.md."""
from __future__ import annotations

import re
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.runs import (
    RunConflict,
    RunRejected,
    cancel_run,
    create_run,
    get_run,
    list_events,
)
from aibroker.auth import ProjectCtx, require_project
from aibroker.services.request_cap import admit_request
from aibroker.telemetry.request_context import REQUEST_ID_HEADER, job_request_id

router = APIRouter(tags=["agent"], prefix="/agent")

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
_TOOL = r"^[\x20-\x7e]+$"


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

    def tools_ok(self) -> bool:
        return all(len(t) <= 128 and re.match(_TOOL, t) is not None for t in self.allowed_tools)


class AgentRunSubmitResponse(BaseModel):
    run_id: int
    status: str
    deduplicated: bool = False
    poll_url: str


class AgentRunResponse(BaseModel):
    run_id: int
    status: str = Field(description="pending|running|completed|failed|cancelled|timed_out")
    capability: str
    task_id: str
    account_id: str
    session_id: str | None = None
    cancel_requested: bool = False
    final_text: str | None = None
    files: list[dict[str, Any]] | None = None
    error: str | None = None


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
    if not ctx.has_scope(AGENT_SCOPE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"project lacks scope: {AGENT_SCOPE}")


def _shape(run: Any, job: Any) -> AgentRunResponse:
    state = run.outcome or ("running" if job.status == "running" else "pending")
    meta = job.result_meta or {}
    return AgentRunResponse(
        run_id=run.run_id, status=state, capability=run.capability, task_id=run.task_id,
        account_id=run.account_id, session_id=run.session_id,
        cancel_requested=run.cancel_requested, final_text=job.result_text,
        files=meta.get("files"), error=job.error_message,
    )


@router.post("/runs", response_model=AgentRunSubmitResponse, status_code=status.HTTP_202_ACCEPTED)
async def agent_run_submit(
    body: AgentRunRequest, response: Response, ctx: ProjectCtx = Depends(require_project),
) -> AgentRunSubmitResponse:
    """Queue an agent run. Owner-only: needs the llm:agent scope."""
    _require_agent_scope(ctx)
    if not body.tools_ok():
        raise HTTPException(422, "allowed_tools entries must be printable ASCII, <=128 chars")
    try:
        run_id, created = await create_run(
            project=ctx.project, capability=body.capability, prompt=body.prompt,
            task_id=body.task_id, account_id=body.account_id, session_id=body.session_id,
            idempotency_key=body.idempotency_key, wall_timeout_s=body.wall_timeout_s,
            idle_timeout_s=body.idle_timeout_s, allowed_tools=body.allowed_tools,
        )
    except RunRejected as e:
        raise HTTPException(400, str(e)) from e
    except RunConflict as e:
        raise HTTPException(409, str(e)) from e
    if created:
        await admit_request(ctx.project)
    response.headers[REQUEST_ID_HEADER] = job_request_id(run_id)
    return AgentRunSubmitResponse(run_id=run_id, status="pending", deduplicated=not created,
                                  poll_url=f"/v1/agent/runs/{run_id}")


@router.get("/runs/{run_id}", response_model=AgentRunResponse)
async def agent_run_get(run_id: int, ctx: ProjectCtx = Depends(require_project)) -> AgentRunResponse:
    _require_agent_scope(ctx)
    hit = await get_run(run_id, ctx.project.id)
    if hit is None:
        raise HTTPException(404, "run not found")
    return _shape(*hit)


@router.get("/runs/{run_id}/events", response_model=AgentEventsResponse)
async def agent_run_events(
    run_id: int, after_seq: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000),
    ctx: ProjectCtx = Depends(require_project),
) -> AgentEventsResponse:
    """Poll events after `after_seq`; pass `next_after_seq` back until `finished`."""
    _require_agent_scope(ctx)
    hit = await get_run(run_id, ctx.project.id)
    if hit is None:
        raise HTTPException(404, "run not found")
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
    return _shape(*hit)
