"""Cancel jobs of one project: a single job, or every in-flight job matching a trace/labels selector.

A pending plain job becomes terminal `cancelled` at once. A running one only gets
`cancel_requested`: the dispatcher checks the flag at attempt boundaries (llm_service.run_chat) and
ends the job `cancelled` — a provider call already in flight may still be billed. `agent:*` jobs go
through the agent run's own cancel (agent/runs.cancel_run).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import NamedTuple

from sqlalchemy import select

from aibroker.agent.runs import RunConflict, cancel_run, get_run
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow
from aibroker.services.deep_jobs import AUDIO_FIELD, get_job

IN_FLIGHT = ("pending", "running")
CANCELLED = "cancelled"
CANCEL_REQUESTED = "cancel_requested"
ALREADY_TERMINAL = "already_terminal"


class AgentCancelForbidden(Exception):
    """The caller's project lacks the agent scope needed to cancel an `agent:*` job."""


class CancelResult(NamedTuple):
    job_id: int
    state: str    # cancelled | cancel_requested | already_terminal
    status: str   # the job's status right now (what a poll reports)


class BulkCancel(NamedTuple):
    cancelled: list[int]
    cancel_requested: list[int]
    already_terminal: int


async def cancel_flag(job_id: int) -> bool:
    """True when a client asked to cancel this job (read by the dispatcher between attempts)."""
    async with get_session() as s:
        return bool((await s.execute(
            select(DeepJobRow.cancel_requested).where(DeepJobRow.id == job_id))).scalar())


async def _cancel_plain(job_id: int, project_id: int) -> CancelResult | None:
    async with get_session() as s:
        # FOR UPDATE: the dispatcher claim (SKIP LOCKED) leaves a row we hold alone, so a pending
        # job is either cancelled here or claimed there - never both.
        row = (await s.execute(
            select(DeepJobRow).where(DeepJobRow.id == job_id, DeepJobRow.project_id == project_id)
            .with_for_update())).scalar_one_or_none()
        if row is None:
            return None
        if row.status == "pending":
            row.status = CANCELLED
            row.completed_at = datetime.now(UTC).replace(tzinfo=None)
            if isinstance(row.request, dict) and row.request.get(AUDIO_FIELD):
                row.request = {**row.request, AUDIO_FIELD: None}   # like _finish: audio is dead weight now
            return CancelResult(job_id, CANCELLED, row.status)
        if row.status == "running":
            row.cancel_requested = True
            return CancelResult(job_id, CANCEL_REQUESTED, row.status)
        return CancelResult(job_id, ALREADY_TERMINAL, row.status)


async def _cancel_agent(job_id: int, project_id: int, status: str) -> CancelResult:
    hit = await get_run(job_id, project_id)
    if hit is None or hit[0].outcome is not None:
        return CancelResult(job_id, ALREADY_TERMINAL, hit[1].status if hit else status)
    try:
        hit = await cancel_run(job_id, project_id)
    except RunConflict:   # finished between the read and the write
        hit = await get_run(job_id, project_id)
    run, job = hit  # type: ignore[misc]  # read a moment ago, cannot have vanished
    if run.outcome == "cancelled":
        return CancelResult(job_id, CANCELLED, job.status)
    if run.outcome is not None:
        return CancelResult(job_id, ALREADY_TERMINAL, job.status)
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        if row is not None:
            row.cancel_requested = True   # poll mirror; the worker watches agent_runs.cancel_requested
    return CancelResult(job_id, CANCEL_REQUESTED, job.status)


async def cancel_job(job_id: int, project_id: int, *, agent_allowed: bool = True) -> CancelResult | None:
    """Cancel one job of `project_id` (idempotent); None when it is not that project's."""
    row = await get_job(job_id, project_id)
    if row is None:
        return None
    if row.capability.startswith("agent:"):
        if not agent_allowed:
            raise AgentCancelForbidden(job_id)
        return await _cancel_agent(job_id, project_id, row.status)
    return await _cancel_plain(job_id, project_id)


async def cancel_matching(
    project_id: int, *, trace_id: str | None = None, labels: dict[str, str] | None = None,
    agent_allowed: bool = True,
) -> BulkCancel:
    """Cancel every NON-terminal job of the project whose trace_id equals `trace_id` / whose labels
    contain all of `labels`. `already_terminal` counts matches that finished before their cancel landed.
    Agent jobs are left alone when the project lacks the agent scope."""
    stmt = select(DeepJobRow.id, DeepJobRow.labels).where(
        DeepJobRow.project_id == project_id, DeepJobRow.status.in_(IN_FLIGHT))
    if trace_id is not None:
        stmt = stmt.where(DeepJobRow.trace_id == trace_id)
    if not agent_allowed:
        stmt = stmt.where(~DeepJobRow.capability.like("agent:%"))
    async with get_session() as s:
        rows = (await s.execute(stmt.order_by(DeepJobRow.id))).all()
    # Labels are matched here, not with JSONB `@>`: the in-flight set is small and this stays one
    # portable path (SQLite tests included).
    wanted = [r[0] for r in rows
              if labels is None or all((r[1] or {}).get(k) == v for k, v in labels.items())]
    out = BulkCancel([], [], 0)
    for job_id in wanted:
        res = await cancel_job(job_id, project_id, agent_allowed=agent_allowed)
        if res is None or res.state == ALREADY_TERMINAL:
            out = out._replace(already_terminal=out.already_terminal + 1)
        elif res.state == CANCELLED:
            out.cancelled.append(job_id)
        else:
            out.cancel_requested.append(job_id)
    return out
