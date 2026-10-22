"""Agent-run service: create (validate, dedupe), read, events, cancel.

A run is a deep_jobs row (capability `agent:*`) plus an agent_runs side row;
the dispatcher for LLM jobs never claims it (job_queue excludes `agent:%`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from aibroker.agent.attachments import strip_attachment_data
from aibroker.agent.events import (
    MAX_TEXT_CHARS,
    OUTCOME_BY_TYPE,
    TERMINAL_TYPES,
    AnyEvent,
    RunCancelled,
    RunWaitingQuota,
)
from aibroker.agent.job_files import store_files
from aibroker.agent.lock import Lease, check_fence
from aibroker.agent.redact import redact, redact_final_text, redact_obj
from aibroker.agent.subscription_job import output_problem
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow, DeepJobRow, ProjectRow
from aibroker.services.deep_jobs import (
    _DEDUP_WINDOW_S,
    PRIORITY_BATCH,
    payload_hash,
    replayed_job_id,
)

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
    idle_timeout_s: int, allowed_tools: list[str], model: str | None = None,
    deadline_s: int = 21600, attachments: list[dict[str, Any]] | None = None,
    run_mode: str = "autonomous", accept_paid_continuation_risk: bool = False,
    client_job: dict[str, Any] | None = None,
    job_idempotency_key: str | None = None, job_payload_hash: str | None = None,
    trace_id: str | None = None, labels: dict[str, str] | None = None,
    priority: int = PRIORITY_BATCH,
) -> tuple[int, bool]:
    """Enqueue a run -> (run_id, created). created=False: an existing run was returned
    (same idempotency_key, or identical payload still in flight).

    `priority` (0 interactive, 1 batch) is the claim order only: it is not in the request or its hash.
    `job_*`/`trace_id`/`labels` are the /v1/jobs contract (deep_jobs columns): a job-level
    Idempotency-Key is checked against `job_payload_hash` (the CLIENT payload hash, free of the
    broker-picked account) and turns the in-flight payload dedup off for this request."""
    request: dict[str, Any] = {
        "prompt": prompt, "task_id": task_id, "account_id": account_id,
        "session_id": session_id, "wall_timeout_s": wall_timeout_s,
        "idle_timeout_s": idle_timeout_s, "allowed_tools": allowed_tools, "model": model,
        "deadline_s": deadline_s, "attachments": attachments or [],
        "run_mode": run_mode, "accept_paid_continuation_risk": accept_paid_continuation_risk,
        "client_job": client_job,
    }
    phash = job_payload_hash or payload_hash(project.id, capability, request)
    if idempotency_key:
        hit = await _find_by_idempotency(project.id, idempotency_key)
        if hit is not None:
            if hit[1].payload_hash != phash:
                raise RunConflict("idempotency_key already used with a different payload")
            return hit[0].run_id, False
    if job_idempotency_key:
        existing = await replayed_job_id(project.id, job_idempotency_key, phash)
        if existing is not None:
            return existing, False
    else:
        existing = await _find_inflight(project.id, capability, phash)
        if existing is not None:
            return existing, False
    if session_id:
        await _check_resume(project.id, capability, account_id, session_id)
    try:
        async with get_session() as s:
            job = DeepJobRow(project_id=project.id, capability=capability, status="pending",
                             request=request, payload_hash=phash, trace_id=trace_id,
                             labels=labels, idempotency_key=job_idempotency_key,
                             priority=priority)
            s.add(job)
            await s.flush()
            s.add(AgentRunRow(
                run_id=job.id, project_id=project.id, capability=capability,
                task_id=task_id, account_id=account_id, resume_session_id=session_id,
                idempotency_key=idempotency_key, model=model,
            ))
            run_id = job.id
    except IntegrityError:   # lost the idempotency_key race: the winner's run is the answer
        if job_idempotency_key:
            winner = await replayed_job_id(project.id, job_idempotency_key, phash)
            if winner is None:
                raise
            return winner, False
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


_MAX_MODEL_OBSERVATIONS = 10
_EVIDENCE_RANK = {"response": 3, "thread_config": 2, "session_init": 1}


def _fold_observation(existing: Any, model: str, kind: str,
                      source: str) -> tuple[list[dict[str, str]], bool]:
    """Fold one observation in: (items, truncated). `truncated` is True when an observation was
    dropped or an entry was evicted to make room (the list is then incomplete)."""
    items = [o for o in (existing or []) if isinstance(o, dict)]
    entry = {"model": model, "kind": kind, "source": source}
    if any(o.get("kind") == kind and o.get("model") == model for o in items):
        return items, False               # one entry per (kind, model)
    if len(items) < _MAX_MODEL_OBSERVATIONS:
        items.append(entry)
        return items, False
    # Full: a stronger kind replaces the weakest entry, so the best evidence is never dropped.
    weakest = min(range(len(items)), key=lambda i: _EVIDENCE_RANK.get(items[i].get("kind"), 0))
    if _EVIDENCE_RANK.get(kind, 0) > _EVIDENCE_RANK.get(items[weakest].get("kind"), 0):
        items[weakest] = entry
    return items, True


def add_model_observation(existing: Any, model: str, kind: str, source: str) -> list[dict[str, str]]:
    """Append {model, kind, source} to the observation list: deduped per (kind, model) and capped
    at 10 (see `_fold_observation` / `observations_truncated` for what the cap drops)."""
    return _fold_observation(existing, model, kind, source)[0]


def served_model_meta(observations: Any, truncated: bool = False) -> dict[str, Any]:
    """result_meta fields for the served model: the strongest evidence wins (response >
    thread_config > session_init, first seen within a kind); nothing observed -> null model with
    kind "unknown". The requested model is never used as a fallback. All observations are kept.
    Top-level `model_served` is strict: set only for response-level evidence; the best weaker
    value lives in `model_observed_best` (shown only inside the poll `evidence`). When the list
    was truncated and more than one distinct `response` model is stored, a dropped observation
    could change the answer, so the top-level value fails closed to null."""
    items = [o for o in (observations or []) if isinstance(o, dict) and o.get("kind") in _EVIDENCE_RANK]
    best = max(items, key=lambda o: _EVIDENCE_RANK[o["kind"]], default=None)
    if best is None:
        return {"model_served": None, "model_observed_best": None, "model_served_kind": "unknown",
                "model_served_source": "none", "model_observations": [],
                "observations_truncated": bool(truncated),
                "observations_complete": not truncated}
    responses = {o["model"] for o in items if o["kind"] == "response"}
    strict = best["kind"] == "response" and not (truncated and len(responses) > 1)
    return {"model_served": best["model"] if strict else None,
            "observations_truncated": bool(truncated), "observations_complete": not truncated,
            "model_observed_best": best["model"], "model_served_kind": best["kind"],
            "model_served_source": best.get("source") or "none", "model_observations": items}


async def append_event(run_id: int, event: AnyEvent, lease: Lease | None = None,
                       *, only_if_pending: bool = False) -> int:
    """Store the next event of a run (single writer per run) -> its seq. Terminal
    events also close the run: outcome set, queue row moved to done/error. With a `lease`
    the write is FENCED: StaleLease if the session was taken over by a newer holder.
    `only_if_pending` makes the write atomic with "nobody claimed it": the queue row is locked
    (FOR UPDATE SKIP LOCKED, like the claim) and must still be `pending`, else RunConflict."""
    now = _now()
    async with get_session() as s:
        await check_fence(s, lease)
        if only_if_pending:
            locked = (await s.execute(
                select(DeepJobRow).where(DeepJobRow.id == run_id)
                .with_for_update(skip_locked=True))).scalar_one_or_none()
            if locked is None or locked.status != "pending":
                raise RunConflict(f"agent run {run_id} was claimed or is locked")
        run = await s.get(AgentRunRow, run_id)
        if run is None:
            raise LookupError(f"agent run {run_id} not found")
        if run.outcome is not None:
            raise RunConflict(f"agent run {run_id} already finished ({run.outcome})")
        # The queue row is only read for events that touch it: usage and model.observed fold
        # into result_meta, terminal events close it. The ~99% hot-path events (message.delta,
        # tool calls/results) load nothing, and `request` (client_job, attachments - can be ~80 KB) is
        # loaded only where it is read: model.observed and terminal events.
        needs_request = event.type == "model.observed" or event.type in TERMINAL_TYPES
        job = (await s.get(DeepJobRow, run_id,
                           options=[] if needs_request else [defer(DeepJobRow.request)])
               if needs_request or event.type in ("usage", "run.started") else None)
        client_job = ((job.request or {}).get("client_job")
                      if job is not None and needs_request else None)
        response_format = client_job.get("response_format") if client_job else None
        final_text = (redact_final_text(event.final_text, response_format)
                      if event.type == "run.completed" else "")
        if event.type == "run.completed" and client_job:
            # Gate the text BEFORE redaction (the model's answer) and the body we will actually
            # return AFTER it: masking must never turn a valid answer into an invalid one.
            problem = (output_problem(event.final_text, response_format)
                       or output_problem(final_text, response_format))
            if problem:
                from aibroker.agent.events import RunFailed
                event = RunFailed(error_class="invalid_json", message=problem)
        run.last_seq += 1
        run.last_event_at = now
        payload = event.model_dump(mode="json")
        if event.type == "run.completed":
            # Keep the event stream metadata-only. Full PNG bytes are returned
            # through the job result, whose retention follows the job purge.
            for file in payload.get("files", []):
                file.pop("content_b64", None)
        s.add(AgentRunEventRow(
            run_id=run_id, seq=run.last_seq, ts=now, type=event.type,
            payload=_clip(redact_obj(payload)),
        ))
        if event.type == "usage" and job is not None:
            meta = dict(job.result_meta or {})
            observed = dict(meta.get("usage_observed") or {})
            for key in ("total_tokens", "reasoning_tokens"):
                value = getattr(event, key)
                if value is not None:
                    observed[key] = (observed.get(key) or 0) + value
            if observed:
                meta["usage_observed"] = observed
                job.result_meta = meta
        if event.type == "model.observed" and job is not None and client_job:
            # The job row is read without a row lock here: the app-server ModelObserved arrives at
            # thread/start, before any tool call, so this unlocked read-modify-write cannot race
            # the client_tools FOR UPDATE path today.
            meta = dict(job.result_meta or {})
            meta["model_observations"], cut = _fold_observation(
                meta.get("model_observations"), event.model, event.kind, event.source)
            if cut:
                meta["observations_truncated"] = True
            job.result_meta = meta
        # the FIRST execution start: a gate requeue or a quota wait must not reset queued_ms
        if (event.type == "run.started" and job is not None
                and not (job.result_meta or {}).get("first_started_at")):
            job.result_meta = {**(job.result_meta or {}), "first_started_at": now.isoformat()}
        if event.type == "session.assigned" and run.session_id is None:
            run.session_id = event.session_id
        if event.type in TERMINAL_TYPES:
            run.outcome = OUTCOME_BY_TYPE[event.type]
            if job is not None:
                first_started_at = (job.result_meta or {}).get("first_started_at")
                history = (job.result_meta or {}).get("client_tool_history") or []
                usage_observed = (job.result_meta or {}).get("usage_observed") or {}
                observations = (job.result_meta or {}).get("model_observations") or []
                truncated = bool((job.result_meta or {}).get("observations_truncated"))
                # every terminal outcome of a subscription job keeps its evidence envelope data
                evidence_meta: dict[str, Any] = {}
                if client_job:
                    evidence_meta = {
                        **served_model_meta(observations, truncated),
                        "account_id": run.account_id, "provider": client_job["provider"],
                        "model": client_job["model"], "billing_type": run.billing_mode,
                        "run_outcome": OUTCOME_BY_TYPE[event.type],
                        "app_server_path": bool(client_job.get("tools"))
                        and run.capability == "agent:codex",
                    }
                job.status = ("done" if event.type == "run.completed"
                              else "cancelled" if event.type == "run.cancelled" else "error")
                job.completed_at = now
                if event.type == "run.completed":
                    job.result_text = final_text
                    # bytes go to deep_job_files in this same transaction (atomic with the result)
                    stored = await store_files(
                        s, run_id, [f.model_dump() for f in event.files],
                        enabled=get_settings().AGENT_FILES_IN_TABLE)
                    job.result_meta = {"files": stored,
                                       "client_tool_history": history,
                                       "usage_observed": usage_observed}
                    if client_job:
                        job.result_meta.update(evidence_meta)
                        job.result_meta.update({
                            "tokens_in": run.usage_input_tokens,
                            "tokens_out": run.usage_output_tokens,
                            "cache_read_tokens": run.usage_cache_read_tokens,
                            "total_tokens": usage_observed.get("total_tokens"),
                            "reasoning_tokens": usage_observed.get("reasoning_tokens"),
                            "cost_usd": None,
                            "finish_reason": "stop",
                        })
                elif event.type == "run.failed":
                    job.result_meta = {"client_tool_history": history,
                                       "usage_observed": usage_observed}
                    job.result_meta.update(evidence_meta)
                    job.result_meta["error_class"] = event.error_class      # -> error_code
                    job.error_message = redact(f"{event.error_class}: {event.message}")
                else:
                    job.result_meta = {"client_tool_history": history,
                                       "usage_observed": usage_observed}
                    job.result_meta.update(evidence_meta)
                    job.error_message = event.type
                if first_started_at:
                    job.result_meta["first_started_at"] = first_started_at
            if isinstance(job.request, dict) and job.request.get("attachments"):
                job.request = {**job.request,
                               "attachments": strip_attachment_data(job.request["attachments"])}
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


async def record_usage(run_id: int, totals: dict[str, int | None], billing_mode: str | None,
                       lease: Lease | None = None) -> None:
    """Keep the CLI-reported usage on the run. Subscription usage lives here, apart from
    API billing (never usage_log); cost_usd stays NULL - no confirmed cost. Fenced like
    append_event."""
    async with get_session() as s:
        await check_fence(s, lease)
        run = await s.get(AgentRunRow, run_id)
        if run is None:
            return
        run.billing_mode = billing_mode
        if any(v is not None for v in totals.values()):
            run.usage_source = "cli_reported"
        for col, key in (("usage_input_tokens", "input_tokens"), ("usage_output_tokens", "output_tokens"),
                         ("usage_cache_read_tokens", "cache_read_tokens"), ("usage_turns", "turns")):
            if totals.get(key) is not None:   # additive: a run may span several attempts
                setattr(run, col, (getattr(run, col) or 0) + totals[key])


async def requeue_run(run_id: int, delay_s: float) -> int:
    """Put a claimed-but-blocked run (session busy) back to pending -> times requeued."""
    async with get_session() as s:
        job = await s.get(DeepJobRow, run_id)
        if job is None:
            return 0
        job.status = "pending"
        job.started_at = None
        job.retry_count += 1
        job.run_after = _now() + timedelta(seconds=delay_s)
        # unlocked read-modify-write, safe by convention: a requeued run has no client tool in
        # flight (it is requeued before it starts), like the model.observed fold above
        job.result_meta = {**(job.result_meta or {}), "pending_reason": "capacity_busy"}
        return job.retry_count


async def park_for_quota(run_id: int, not_before: datetime, reset_reported: bool = False,
                         lease: Lease | None = None, reason: str = "") -> int:
    """The CLI hit its usage limit: record `run.waiting_quota`, put the run back to pending
    until `not_before`, and resume THIS run's own session on the retry. Same account, same
    billing route -> number of quota waits so far. Fenced like append_event."""
    now = _now()
    async with get_session() as s:
        await check_fence(s, lease)
        run = await s.get(AgentRunRow, run_id)
        job = await s.get(DeepJobRow, run_id)
        if run is None or job is None or run.outcome is not None:
            raise RunConflict(f"agent run {run_id} is not waiting-capable")
        run.quota_waits += 1
        run.last_seq += 1
        run.last_event_at = now
        s.add(AgentRunEventRow(
            run_id=run_id, seq=run.last_seq, ts=now, type="run.waiting_quota",
            payload=_waiting_payload(not_before, run.quota_waits, reset_reported, reason)))
        if run.session_id:
            run.resume_session_id = run.session_id
        job.status = "pending"
        job.started_at = None
        job.run_after = not_before
        # unlocked read-modify-write, safe by convention: the worker parking the run is the only
        # writer now - submit_result only fires while that worker waits for a tool, not here
        job.result_meta = {**(job.result_meta or {}), "pending_reason": "waiting_quota"}
        return run.quota_waits


def _waiting_payload(not_before: datetime, attempt: int, reset_reported: bool,
                     reason: str = "") -> dict[str, Any]:
    return RunWaitingQuota(not_before=not_before.replace(tzinfo=UTC).isoformat(),
                           attempt=attempt, reset_reported=reset_reported,
                           reason=reason[:300]).model_dump(mode="json")


async def get_attestation(run_id: int) -> dict[str, Any] | None:
    """The latest `billing.attested` payload of a run (what the broker checked), or None when
    the run never got past the billing guard."""
    async with get_session() as s:
        row = (await s.execute(
            select(AgentRunEventRow).where(AgentRunEventRow.run_id == run_id,
                                           AgentRunEventRow.type == "billing.attested")
            .order_by(AgentRunEventRow.seq.desc()).limit(1))).scalar_one_or_none()
    return dict(row.payload) if row else None
