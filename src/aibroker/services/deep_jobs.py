"""Async job wrapper around capability=chat:deep.

Why this exists: nemotron-3-ultra (the only chat:deep provider) has been
observed taking up to ~8 minutes on NVIDIA's free, oversubscribed pool.
Cloudflare's edge (~100s) and this broker's own nginx (proxy_read_timeout
120s, see infra/nginx-aib.conf) both time out well before that — the
client got a 504 while the broker was still waiting on the provider and
would eventually log a perfectly good "ok" that nobody was left to see.

Submit creates a `deep_jobs` row and schedules the real call in the
background (asyncio.create_task on whichever of the 2 uvicorn workers
handled the submit) — the HTTP response returns immediately. Poll reads
the job row from Postgres, so it works no matter which worker answers the
poll request; no in-process task handle needs to survive across workers.

A worker restart mid-job leaves the row stuck at "pending" — `get_job`
lazily marks anything past `_STALE_AFTER_S` as a timeout error instead of
needing a separate sweeper process.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.services.llm_service import run_chat

log = logging.getLogger(__name__)

# Longest observed real call was ~8 min; give real margin before calling a
# stuck "pending" row a timeout rather than a still-legitimately-running job.
_STALE_AFTER_S = 20 * 60


async def submit_deep_job(
    *,
    project: ProjectRow,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    workflow: str | None,
) -> int:
    """Create a pending job row, schedule the real call in the background,
    return the job id immediately."""
    request = {
        "messages": messages, "model": model,
        "max_tokens": max_tokens, "temperature": temperature,
        "workflow": workflow,
    }
    async with get_session() as s:
        row = DeepJobRow(project_id=project.id, status="pending", request=request)
        s.add(row)
        await s.flush()
        job_id = row.id
    asyncio.create_task(_run_job(job_id, project, request))
    return job_id


async def _run_job(job_id: int, project: ProjectRow, request: dict[str, Any]) -> None:
    try:
        outcome = await run_chat(
            project=project, capability="chat:deep",
            messages=request["messages"], model=request["model"],
            max_tokens=request["max_tokens"], temperature=request["temperature"],
            response_format=None,  # nemotron isn't JSON-reliable — see chains.py
            workflow=request["workflow"],
        )
    except Exception as e:  # noqa: BLE001 — any failure must still resolve the job
        log.warning("deep_job %d failed: %s", job_id, e)
        await _finish(job_id, status="error", error_message=str(e))
        return
    if outcome is None:
        await _finish(job_id, status="error",
                       error_message="no provider available for capability=chat:deep")
        return
    await _finish(
        job_id, status="done", result_text=outcome.text,
        result_meta={
            "provider": outcome.provider, "model": outcome.model,
            "tokens_in": outcome.tokens_in, "tokens_out": outcome.tokens_out,
            "cost_usd": outcome.cost_usd, "latency_ms": outcome.latency_ms,
            "key_label": outcome.key_label, "request_id": outcome.request_id,
            "cache_read_tokens": outcome.cache_read_tokens,
            "cache_write_tokens": outcome.cache_write_tokens,
        },
    )


async def _finish(
    job_id: int, *, status: str,
    result_text: str | None = None,
    result_meta: dict[str, Any] | None = None,
    error_message: str | None = None,
) -> None:
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        if row is None:  # pragma: no cover — job row deleted underneath us
            return
        row.status = status
        row.result_text = result_text
        row.result_meta = result_meta
        row.error_message = error_message
        row.completed_at = datetime.now(UTC).replace(tzinfo=None)


async def get_job(job_id: int, project_id: int) -> DeepJobRow | None:
    """Fetch a job, scoped to the caller's own project. Lazily resolves a
    stuck "pending" row into a timeout error instead of polling forever."""
    async with get_session() as s:
        row = (await s.execute(
            select(DeepJobRow).where(
                DeepJobRow.id == job_id, DeepJobRow.project_id == project_id
            )
        )).scalar_one_or_none()
        if row is None:
            return None
        if row.status == "pending":
            age_s = (datetime.now(UTC).replace(tzinfo=None) - row.created_at).total_seconds()
            if age_s > _STALE_AFTER_S:
                row.status = "error"
                row.error_message = (
                    f"timed out after {int(age_s)}s with no result — the worker "
                    "handling this job likely restarted mid-call"
                )
                row.completed_at = datetime.now(UTC).replace(tzinfo=None)
        return row


def next_poll_after_s(created_at: datetime) -> int:
    """Suggested poll interval — start slow (this is a minutes-long job, not
    a fast one), don't hammer the endpoint. Widens with a small cap so a
    long-pending job doesn't get polled every second nor once a minute."""
    age = (datetime.now(UTC).replace(tzinfo=None) - created_at)
    if age < timedelta(seconds=30):
        return 5
    if age < timedelta(minutes=2):
        return 10
    return 20
