"""Dedicated agent worker lane: claims only `agent:*` rows, with its own per-capability
concurrency (default 1) that never shares slots with the LLM job dispatcher."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from aibroker.agent import events as ev
from aibroker.agent.constants import AGENT_CAPABILITIES
from aibroker.agent.executor import ExecConfig, exec_config_from_settings, execute_run
from aibroker.agent.real_cli import register_real_adapters
from aibroker.agent.runs import RunConflict, append_event
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow

log = logging.getLogger(__name__)

STALE_GRACE_S = 300.0


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def concurrency_for(capability: str) -> int:
    s = get_settings()
    n = s.AGENT_CONCURRENCY_CODEX if capability == "agent:codex" else s.AGENT_CONCURRENCY_CLAUDE
    return max(0, n)


async def claim_agent_runs(capability: str, limit: int) -> list[int]:
    """Atomically move up to `limit` eligible pending runs of ONE capability to running."""
    if limit <= 0:
        return []
    now = _now()
    async with get_session() as s:
        rows = (await s.execute(
            select(DeepJobRow).where(
                DeepJobRow.capability == capability, DeepJobRow.status == "pending",
                (DeepJobRow.run_after.is_(None)) | (DeepJobRow.run_after < now),
            ).order_by(DeepJobRow.created_at).limit(limit).with_for_update(skip_locked=True)
        )).scalars().all()
        for r in rows:
            r.status = "running"
            r.started_at = now
        return [r.id for r in rows]


async def fail_stale_agent_runs() -> int:
    """A `running` run whose worker died is FAILED (worker_lost), never re-run: a CLI run
    has side effects. Deadline = started_at + its wall timeout + grace."""
    now = _now()
    async with get_session() as s:
        rows = (await s.execute(
            select(DeepJobRow).where(DeepJobRow.status == "running",
                                     DeepJobRow.capability.like("agent:%"))
        )).scalars().all()
        stale = [r.id for r in rows if r.started_at is not None and now > r.started_at
                 + timedelta(seconds=float(r.request.get("wall_timeout_s", 1800)) + STALE_GRACE_S)]
    failed = 0
    for run_id in stale:
        try:
            await append_event(run_id, ev.RunFailed(
                error_class="worker_lost", message="worker died or overran its deadline"))
            failed += 1
        except RunConflict:
            pass
    return failed


async def drain_agent_once(cfg: ExecConfig) -> int:
    """One pass: claim within each capability's free slots and run them to completion
    (deterministic; used by tests). Returns how many were claimed."""
    total = 0
    for cap in AGENT_CAPABILITIES:
        ids = await claim_agent_runs(cap, concurrency_for(cap))
        total += len(ids)
        await asyncio.gather(*(execute_run(i, cfg) for i in ids), return_exceptions=True)
    return total


async def _guarded(run_id: int, cfg: ExecConfig) -> None:  # pragma: no cover
    try:
        await execute_run(run_id, cfg)
    except Exception:  # noqa: BLE001 - left `running`; fail_stale_agent_runs closes it
        log.exception("agent run %d crashed", run_id)


async def agent_worker_loop(stop: asyncio.Event) -> None:  # pragma: no cover - loop harness
    """Forever: keep up to concurrency_for(cap) runs in flight per capability."""
    register_real_adapters()
    cfg = exec_config_from_settings()
    inflight: dict[str, set[asyncio.Task[None]]] = {c: set() for c in AGENT_CAPABILITIES}
    log.info("agent worker started (workspace=%s)", cfg.workspace_root)
    while not stop.is_set():
        try:
            await fail_stale_agent_runs()
            for cap in AGENT_CAPABILITIES:
                free = concurrency_for(cap) - len(inflight[cap])
                for run_id in await claim_agent_runs(cap, free):
                    t = asyncio.create_task(_guarded(run_id, cfg))
                    inflight[cap].add(t)
                    t.add_done_callback(inflight[cap].discard)
        except Exception:  # noqa: BLE001 - a bad tick must not kill the lane
            log.exception("agent worker tick failed")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=1.0)
    pending = [t for ts in inflight.values() for t in ts]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    log.info("agent worker stopped")
