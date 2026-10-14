"""Cancel async jobs: POST /v1/jobs/{id}/cancel and POST /v1/jobs/cancel (by trace_id / labels).

Own project only; the same project key as submit/poll. See services/job_control.py for the semantics.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from aibroker.agent.constants import AGENT_SCOPE, project_allowed
from aibroker.auth import ProjectCtx, require_project
from aibroker.services.deep_jobs import IdempotencyConflict, replayed_job_id
from aibroker.services.job_control import AgentCancelForbidden, cancel_job, cancel_matching
from aibroker.services.job_list import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, list_jobs
from aibroker.services.job_meta import check_labels, check_token

router = APIRouter(tags=["jobs"])


def idempotency_conflict(exc: IdempotencyConflict) -> HTTPException:
    return HTTPException(409, detail={
        "error": "idempotency_key_conflict", "existing_job_id": exc.existing_job_id,
        "message": "Idempotency-Key was already used with a different payload"})


async def known_job_id(project_id: int, key: str | None, phash: str | None) -> int | None:
    """The job an Idempotency-Key already created for this payload (None: new key or no key);
    HTTP 409 when the key was used with a different payload."""
    if not key or phash is None:
        return None
    try:
        return await replayed_job_id(project_id, key, phash)
    except IdempotencyConflict as exc:
        raise idempotency_conflict(exc) from exc


class CancelResponse(BaseModel):
    job_id: int
    state: Literal["cancelled", "cancel_requested", "already_terminal"] = Field(
        description="cancelled: terminal now | cancel_requested: running, outcome via poll | "
                    "already_terminal: nothing to cancel")
    status: str = Field(description="the job's status right now, as a poll reports it")


class BulkCancelRequest(BaseModel):
    trace_id: str | None = None
    labels: dict[str, str] | None = None

    @field_validator("trace_id")
    @classmethod
    def _trace(_cls, value: str | None) -> str | None:
        return None if value is None else check_token(value, "trace_id")

    @field_validator("labels")
    @classmethod
    def _labels(_cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return check_labels(value)

    @model_validator(mode="after")
    def _one_selector(self) -> BulkCancelRequest:
        if (self.trace_id is None) == (not self.labels):
            raise ValueError("give exactly one selector: trace_id or a non-empty labels object")
        return self


class BulkCancelResponse(BaseModel):
    cancelled: list[int]
    cancel_requested: list[int]
    already_terminal: int


@router.post("/jobs/cancel", response_model=BulkCancelResponse)
async def jobs_cancel_bulk(
    body: BulkCancelRequest, ctx: ProjectCtx = Depends(require_project),
) -> BulkCancelResponse:
    """Cancel every non-terminal job of this project matching `trace_id` or `labels`."""
    out = await cancel_matching(ctx.project.id, trace_id=body.trace_id, labels=body.labels,
                                agent_allowed=ctx.has_scope(AGENT_SCOPE))
    return BulkCancelResponse(cancelled=out.cancelled, cancel_requested=out.cancel_requested,
                              already_terminal=out.already_terminal)


@router.post("/jobs/{job_id}/cancel", response_model=CancelResponse)
async def jobs_cancel(job_id: int, ctx: ProjectCtx = Depends(require_project)) -> CancelResponse:
    """Cancel one job (idempotent). 404 when it is not this project's."""
    try:
        res = await cancel_job(job_id, ctx.project.id, agent_allowed=ctx.has_scope(AGENT_SCOPE))
    except AgentCancelForbidden as exc:
        raise HTTPException(403, f"project lacks scope: {AGENT_SCOPE}") from exc
    if res is None:
        raise HTTPException(404, "job not found")
    return CancelResponse(job_id=job_id, state=res.state, status=res.status)  # type: ignore[arg-type]


class JobListItem(BaseModel):
    """Metadata only: never the request, prompts, result or error text."""
    job_id: int
    status: str
    capability: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    labels: dict[str, str] | None
    trace_id: str | None
    idempotency_key: str | None
    retry_count: int
    cancel_requested: bool


class JobListResponse(BaseModel):
    jobs: list[JobListItem]
    next_cursor: str | None = Field(description="pass as `cursor` for the next page; null on the last")
    pagination_complete: bool = Field(description="no more pages within the high watermark")
    no_nonterminal_in_filter: bool = Field(
        description="across the WHOLE label/trace_id-filtered set up to the watermark (status filter "
                    "ignored): no pending/running job")
    high_watermark: int = Field(description="highest job id of the project when the walk began; "
                                            "jobs created later are not listed")
    retention_days: int = Field(description="finished jobs older than this are purged")
    as_of: datetime


def _agent_reader(ctx: ProjectCtx) -> Callable[[str, dict[str, Any]], bool]:
    """Per-row agent read gate for the list: exactly the poll route's re-authorization."""
    from aibroker.routes.proxy import _require_agent_read_access  # lazy: proxy imports this module

    def readable(capability: str, request: dict[str, Any]) -> bool:
        try:
            _require_agent_read_access(ctx, SimpleNamespace(capability=capability, request=request))
        except Exception:   # 403 / allowlist / policy: the same refusals a poll would answer
            return False
        return True
    return readable


@router.get("/jobs", response_model=JobListResponse)
async def jobs_list(
    request: Request,
    trace_id: str | None = None,
    status: Literal["pending", "running", "done", "error", "cancelled"] | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = None,
    ctx: ProjectCtx = Depends(require_project),
) -> JobListResponse:
    """List this project's jobs carrying exactly one label pair (`label.<key>=<value>`)."""
    pairs = [(k, v) for k, v in request.query_params.multi_items() if k.startswith("label.")]
    extra = {k for k in request.query_params if not k.startswith("label.")} \
        - {"trace_id", "status", "limit", "cursor"}
    if len(pairs) != 1 or extra:
        raise HTTPException(422, "give exactly one label.<key>=<value> pair"
                                 + (f"; unknown parameters: {sorted(extra)}" if extra else ""))
    try:
        checked = check_labels({pairs[0][0].removeprefix("label."): pairs[0][1]}) or {}
        (key, value), = checked.items()
        trace = None if trace_id is None else check_token(trace_id, "trace_id")
        after = None if cursor is None else decode_cursor(cursor)
    except ValueError as exc:   # BadCursor is a ValueError
        raise HTTPException(422, str(exc)) from exc
    page = await list_jobs(
        ctx.project.id, key, value, trace_id=trace, status=status, limit=limit, cursor=after,
        agent_open=ctx.has_scope(AGENT_SCOPE) and project_allowed(ctx.project.id),
        agent_readable=_agent_reader(ctx))
    return JobListResponse(**page._asdict())
