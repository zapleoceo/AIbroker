"""Agent-run service: create (validate, dedupe), read, events, cancel.

A run is a deep_jobs row (capability `agent:*`) plus an agent_runs side row;
the dispatcher for LLM jobs never claims it (job_queue excludes `agent:%`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from aibroker.agent.events import (
    MAX_TEXT_CHARS,
    OUTCOME_BY_TYPE,
    TERMINAL_TYPES,
    AnyEvent,
    RunCancelled,
)
from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow, DeepJobRow, ProjectRow
from aibroker.services.deep_jobs import _DEDUP_WINDOW_S, payload_hash

IN_FLIGHT = ("pending", "running")


class RunConflict(Exception):
    """The request clashes with existing state (HTTP 409)."""


class RunRejected(Exception):
    """The request is not allowed as stated (HTTP 400)."""


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _find_by_idempotency(project_id: int, key: str) -> tuple[AgentRunRow, DeepJobRow] | None:
    async with get_session() as s:
        row = (await s.execute(
            select(AgentRunRow, DeepJobRow)
            .join(DeepJobRow, DeepJobRow.id == AgentRunRow.run_id)
            .where(AgentRunRow.project_id == project_id, AgentRunRow.idempotency_key == key)
        )).first()
    return (row[0], row[1]) if row else None


async def _find_inflight(project_id: int, capability: str, phash: str) -> int | None:
    cutoff = _now() - timedelta(seconds=_DEDUP_WINDOW_S)
    async with get_session() as s:
        return (await s.execute(
            select(DeepJobRow.id).where(
                DeepJobRow.project_id == project_id, DeepJobRow.capability == capability,
                DeepJobRow.payload_hash == phash, DeepJobRow.status.in_(IN_FLIGHT),
                DeepJobRow.created_at > cutoff,
            ).order_by(DeepJobRow.created_at.desc()).limit(1)
        )).scalars().first()


async def _check_resume(project_id: int, capability: str, account_id: str, session_id: str) -> None:
    """Resume only an EXACT session a previous run of this project produced for
    the SAME account; never one that is still in use. No "last"/"continue"."""
    async with get_session() as s:
        owners = (await s.execute(
            select(AgentRunRow.account_id, DeepJobRow.status)
            .join(DeepJobRow, DeepJobRow.id == AgentRunRow.run_id)
            .where(AgentRunRow.project_id == project_id,
                   AgentRunRow.capability == capability,
                   AgentRunRow.session_id == session_id)
        )).all()
    if not owners:
        raise RunRejected("session_id was not produced by a previous run of this project")
    if any(o[0] != account_id for o in owners):
        raise RunRejected("session_id belongs to a different account_id")
    if any(o[1] in IN_FLIGHT for o in owners):
        raise RunConflict("session_id is in use by a run that has not finished")


async def create_run(
    *, project: ProjectRow, capability: str, prompt: str, task_id: str, account_id: str,
    session_id: str | None, idempotency_key: str | None, wall_timeout_s: int,
    idle_timeout_s: int, allowed_tools: list[str],
) -> tuple[int, bool]:
    """Enqueue a run -> (run_id, created). created=False: an existing run was returned
    (same idempotency_key, or identical payload still in flight)."""
    request: dict[str, Any] = {
        "prompt": prompt, "task_id": task_id, "account_id": account_id,
        "session_id": session_id, "wall_timeout_s": wall_timeout_s,
        "idle_timeout_s": idle_timeout_s, "allowed_tools": allowed_tools,
    }
    phash = payload_hash(project.id, capability, request)
    if idempotency_key:
        hit = await _find_by_idempotency(project.id, idempotency_key)
        if hit is not None:
            if hit[1].payload_hash != phash:
                raise RunConflict("idempotency_key already used with a different payload")
            return hit[0].run_id, False
    existing = await _find_inflight(project.id, capability, phash)
    if existing is not None:
        return existing, False
    if session_id:
        await _check_resume(project.id, capability, account_id, session_id)
    try:
        async with get_session() as s:
            job = DeepJobRow(project_id=project.id, capability=capability, status="pending",
                             request=request, payload_hash=phash)
            s.add(job)
            await s.flush()
            s.add(AgentRunRow(
                run_id=job.id, project_id=project.id, capability=capability,
                task_id=task_id, account_id=account_id, resume_session_id=session_id,
                idempotency_key=idempotency_key,
            ))
            run_id = job.id
    except IntegrityError:   # lost the idempotency_key race: the winner's run is the answer
        hit = await _find_by_idempotency(project.id, idempotency_key) if idempotency_key else None
        if hit is None:
            raise
        return hit[0].run_id, False
    return run_id, True


async def get_run(run_id: int, project_id: int) -> tuple[AgentRunRow, DeepJobRow] | None:
    """The run, scoped to the caller's project (None when absent or not theirs)."""
    async with get_session() as s:
        row = (await s.execute(
            select(AgentRunRow, DeepJobRow)
            .join(DeepJobRow, DeepJobRow.id == AgentRunRow.run_id)
            .where(AgentRunRow.run_id == run_id, AgentRunRow.project_id == project_id)
        )).first()
    return (row[0], row[1]) if row else None


async def list_events(run_id: int, after_seq: int = 0, limit: int = 500) -> list[AgentRunEventRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(AgentRunEventRow)
            .where(AgentRunEventRow.run_id == run_id, AgentRunEventRow.seq > after_seq)
            .order_by(AgentRunEventRow.seq).limit(limit)
        )).scalars())


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_TEXT_CHARS]
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clip(v) for v in value]
    return value


async def append_event(run_id: int, event: AnyEvent) -> int:
    """Store the next event of a run (single writer per run) -> its seq. Terminal
    events also close the run: outcome set, queue row moved to done/error."""
    now = _now()
    async with get_session() as s:
        run = await s.get(AgentRunRow, run_id)
        if run is None:
            raise LookupError(f"agent run {run_id} not found")
        if run.outcome is not None:
            raise RunConflict(f"agent run {run_id} already finished ({run.outcome})")
        run.last_seq += 1
        run.last_event_at = now
        s.add(AgentRunEventRow(
            run_id=run_id, seq=run.last_seq, ts=now, type=event.type,
            payload=_clip(event.model_dump(mode="json")),
        ))
        if event.type == "session.assigned" and run.session_id is None:
            run.session_id = event.session_id
        if event.type in TERMINAL_TYPES:
            run.outcome = OUTCOME_BY_TYPE[event.type]
            job = await s.get(DeepJobRow, run_id)
            if job is not None:
                job.status = "done" if event.type == "run.completed" else "error"
                job.completed_at = now
                if event.type == "run.completed":
                    job.result_text = event.final_text
                    job.result_meta = {"files": [f.model_dump() for f in event.files]}
                elif event.type == "run.failed":
                    job.error_message = f"{event.error_class}: {event.message}"
                else:
                    job.error_message = event.type
        return run.last_seq


async def cancel_run(run_id: int, project_id: int) -> tuple[AgentRunRow, DeepJobRow] | None:
    """Request cancellation. A queued run is cancelled on the spot; a running one is
    flagged for the worker to stop; a finished one is returned unchanged."""
    hit = await get_run(run_id, project_id)
    if hit is None:
        return None
    run, job = hit
    if run.outcome is None:
        if job.status == "pending":
            await append_event(run_id, RunCancelled())
        else:
            async with get_session() as s:
                row = await s.get(AgentRunRow, run_id)
                if row is not None:
                    row.cancel_requested = True
    return await get_run(run_id, project_id)
