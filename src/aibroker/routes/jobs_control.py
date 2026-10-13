"""Cancel async jobs: POST /v1/jobs/{id}/cancel and POST /v1/jobs/cancel (by trace_id / labels).

Own project only; the same project key as submit/poll. See services/job_control.py for the semantics.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from aibroker.agent.constants import AGENT_SCOPE
from aibroker.auth import ProjectCtx, require_project
from aibroker.services.job_control import AgentCancelForbidden, cancel_job, cancel_matching
from aibroker.services.job_meta import check_labels, check_token

router = APIRouter(tags=["jobs"])


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
    def _trace(cls, value: str | None) -> str | None:
        return None if value is None else check_token(value, "trace_id")

    @field_validator("labels")
    @classmethod
    def _labels(cls, value: dict[str, str] | None) -> dict[str, str] | None:
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
