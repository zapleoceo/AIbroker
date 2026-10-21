"""Dedicated agent worker lane: claims only `agent:*` rows, with its own per-capability
concurrency (default 1) that never shares slots with the LLM job dispatcher."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from aibroker.agent import events as ev
from aibroker.agent.constants import AGENT_CAPABILITIES
from aibroker.agent.executor import ExecConfig, exec_config_from_settings, execute_run
from aibroker.agent.real_cli import register_real_adapters
from aibroker.agent.runs import RunConflict, append_event
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow, DeepJobRow
from aibroker.services.deep_jobs import PRIORITY_INTERACTIVE

log = logging.getLogger(__name__)

STALE_GRACE_S = 300.0
# Housekeeping (stale / overdue-waiting / unclaimed sweeps) deals in minutes-to-hours
# deadlines, so it runs on this cadence; only the claim runs on every tick.
HOUSEKEEPING_EVERY_S = 30.0


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


WORKER_MARKER = "/etc/aibroker-agent-worker"


_REQUIRED_HOME = {"agent:claude": "CLAUDE_CONFIG_DIR", "agent:codex": "CODEX_HOME"}


def worker_capabilities() -> tuple[str, ...]:
    """The capabilities this worker claims (AGENT_WORKER_CAPABILITIES, known ones only)."""
    wanted = {c.strip() for c in get_settings().AGENT_WORKER_CAPABILITIES.split(",")}
    return tuple(c for c in AGENT_CAPABILITIES if c in wanted)


def worker_image_problem(env: Mapping[str, str] | None = None, marker: str = WORKER_MARKER,
                         capabilities: Iterable[str] = ("agent:claude",),
                         account_id: str | None = None) -> str | None:
    """The agent lane may only run inside the worker image (CLI + auth volume + minimal
    privileges); anywhere else it refuses to start. `account_id` (the worker's
    AGENT_ACCOUNT_ID) is checked only when given: a Claude worker must name its account, or it
    would claim runs of every login."""
    e = os.environ if env is None else env
    if account_id is not None and "agent:claude" in capabilities and not account_id.strip():
        return "AGENT_ACCOUNT_ID is not set (a Claude worker must name its account)"
    for cap in capabilities:
        home = _REQUIRED_HOME.get(cap)
        if home and not e.get(home):
            return f"{home} is not set"
    if not Path(marker).exists():
        return f"{marker} is missing - not the agent-worker image"
    return None


def concurrency_for(capability: str) -> int:
    s = get_settings()
    n = s.AGENT_CONCURRENCY_CODEX if capability == "agent:codex" else s.AGENT_CONCURRENCY_CLAUDE
    return max(0, n)


def effective_interactive_reserve(reserve: int, concurrency: int) -> int:
    """Slots kept for priority-0 (interactive) runs: AGENT_INTERACTIVE_RESERVE, capped at
    concurrency - 1 so a lane never reserves itself out of batch work (concurrency 1 -> 0)."""
    return max(0, min(reserve, concurrency - 1))


async def claim_agent_runs(capability: str, limit: int, *,
                           interactive_only: bool = False) -> list[int]:
    """Atomically move up to `limit` eligible pending runs of ONE capability to running,
    interactive (priority 0) before batch, oldest first within a priority.
    `interactive_only` skips batch runs (the reserved slots)."""
    if limit <= 0:
        return []
    now = _now()
    stmt = select(DeepJobRow).where(
        DeepJobRow.capability == capability, DeepJobRow.status == "pending",
        (DeepJobRow.run_after.is_(None)) | (DeepJobRow.run_after < now))
    if interactive_only:
        stmt = stmt.where(DeepJobRow.priority == PRIORITY_INTERACTIVE)
    if account := get_settings().AGENT_ACCOUNT_ID.strip():
        # One worker = one subscription login: claim only that account's runs. An IN-subquery on
        # the agent_runs primary key (no join, so FOR UPDATE still locks only deep_jobs rows and
        # the ix_deep_jobs_claim_order scan narrows first).
        stmt = stmt.where(DeepJobRow.id.in_(
            select(AgentRunRow.run_id).where(AgentRunRow.account_id == account)))
    async with get_session() as s:
        rows = (await s.execute(
            stmt.order_by(DeepJobRow.priority, DeepJobRow.created_at).limit(limit).with_for_update(skip_locked=True)
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
        # Only the columns the deadline needs: never the whole row with its request JSON.
        rows = (await s.execute(
            select(DeepJobRow.id, DeepJobRow.started_at,
                   DeepJobRow.request["wall_timeout_s"]).where(
                DeepJobRow.status == "running", DeepJobRow.capability.like("agent:%"))
        )).all()
        stale = [run_id for run_id, started_at, wall in rows if started_at is not None
                 and now > started_at + timedelta(
                     seconds=float(1800 if wall is None else wall) + STALE_GRACE_S)]
    failed = 0
    for run_id in stale:
        try:
            await append_event(run_id, ev.RunFailed(
                error_class="worker_lost", message="worker died or overran its deadline"))
            failed += 1
        except RunConflict:
            pass
    return failed


async def expire_overdue_waiting() -> int:
    """A run waiting for quota past its overall deadline ends `run.failed` (quota_exhausted) with
    the limit wording the CLI gave (kept on its last `run.waiting_quota` event)."""
    now = _now()
    async with get_session() as s:
        rows = (await s.execute(
            select(DeepJobRow.id, DeepJobRow.created_at, DeepJobRow.request["deadline_s"])
            .join(AgentRunRow, AgentRunRow.run_id == DeepJobRow.id).where(
                DeepJobRow.status == "pending", AgentRunRow.quota_waits > 0,
                AgentRunRow.outcome.is_(None))
        )).all()
        overdue = [run_id for run_id, created_at, deadline in rows if now >= created_at
                   + timedelta(seconds=float(21600 if deadline is None else deadline))]
    done = 0
    for run_id in overdue:
        with contextlib.suppress(RunConflict):
            async with get_session() as s:
                reason = (await s.execute(
                    select(AgentRunEventRow.payload).where(
                        AgentRunEventRow.run_id == run_id,
                        AgentRunEventRow.type == "run.waiting_quota")
                    .order_by(AgentRunEventRow.seq.desc()).limit(1))).scalar() or {}
            await append_event(run_id, ev.RunFailed(
                error_class="quota_exhausted",
                message=f"{reason.get('reason') or 'usage limit reached'} "
                        "(quota wait is past the job deadline)",
                # the stored reset keeps the account paused for new-run selection (accounts.py)
                reset_at=reason.get("not_before") if reason.get("reset_reported") else None))
            done += 1
    return done


async def drain_agent_once(cfg: ExecConfig) -> int:
    """One pass: claim within each capability's free slots and run them to completion
    (deterministic; used by tests). Returns how many were claimed."""
    total = 0
    for cap in worker_capabilities():
        ids = await claim_agent_runs(cap, concurrency_for(cap))
        total += len(ids)
        await asyncio.gather(*(execute_run(i, cfg) for i in ids), return_exceptions=True)
    return total


async def _guarded(run_id: int, cfg: ExecConfig) -> None:  # pragma: no cover
    try:
        await execute_run(run_id, cfg)
    except Exception:  # noqa: BLE001 - left `running`; fail_stale_agent_runs closes it
        log.exception("agent run %d crashed", run_id)


async def fail_unclaimed_runs() -> int:
    """A run nobody claimed within AGENT_PENDING_MAX_S because NO worker is alive for its account
    ends `run.failed` account_unavailable instead of waiting forever. Only never-started runs
    (no events, no quota wait) are candidates, and only when the account has no `running` run and
    no run that started within the window (a run queued behind a long run on a live worker is
    never reaped). The failure is written atomically with a still-`pending` check."""
    limit = get_settings().AGENT_PENDING_MAX_S
    if limit <= 0:
        return 0
    cutoff = _now() - timedelta(seconds=limit)
    async with get_session() as s:
        rows = (await s.execute(
            select(DeepJobRow.id, AgentRunRow.account_id, DeepJobRow.capability)
            .join(AgentRunRow, AgentRunRow.run_id == DeepJobRow.id).where(
                DeepJobRow.status == "pending", DeepJobRow.capability.like("agent:%"),
                DeepJobRow.created_at < cutoff, AgentRunRow.last_seq == 0,
                AgentRunRow.quota_waits == 0, AgentRunRow.outcome.is_(None))
        )).all()
        live = set((await s.execute(
            select(AgentRunRow.account_id, DeepJobRow.capability)
            .join(DeepJobRow, DeepJobRow.id == AgentRunRow.run_id).where(
                DeepJobRow.capability.like("agent:%"),
                (DeepJobRow.status == "running") | (DeepJobRow.started_at >= cutoff)
                | (DeepJobRow.completed_at >= cutoff) | (AgentRunRow.last_event_at >= cutoff))
            .distinct())).all())
    done = 0
    for run_id, account, capability in rows:
        if (account, capability) in live:
            continue
        with contextlib.suppress(RunConflict):
            await append_event(run_id, ev.RunFailed(
                error_class="account_unavailable",
                message=f"no worker claimed this run for account {account} "
                        f"within {int(limit)}s"), only_if_pending=True)
            done += 1
    return done


async def _wait_stop_or_wake(stop: asyncio.Event | None, wake: asyncio.Event,
                             timeout: float) -> None:  # pragma: no cover — loop harness
    """Sleep until a wake-up (RPC nudge or a finished run), shutdown, or the timeout."""
    waiters = [asyncio.create_task(wake.wait())]
    if stop is not None:
        waiters.append(asyncio.create_task(stop.wait()))
    try:
        await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waiters:
            w.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)


async def agent_worker_loop(stop: asyncio.Event,
                            wake: asyncio.Event | None = None) -> None:  # pragma: no cover
    """Forever: keep up to concurrency_for(cap) runs in flight per capability."""
    caps = worker_capabilities()
    problem = worker_image_problem(
        capabilities=caps, account_id=get_settings().AGENT_ACCOUNT_ID) if caps else "AGENT_WORKER_CAPABILITIES is empty"
    if problem:
        raise RuntimeError(f"agent lane refused to start: {problem}")
    register_real_adapters(caps)
    cfg = exec_config_from_settings()
    inflight: dict[str, set[asyncio.Task[None]]] = {c: set() for c in caps}
    log.info("agent worker started (workspace=%s)", cfg.workspace_root)
    last_housekeeping = float("-inf")       # first tick (startup) always sweeps
    refill = wake if wake is not None else asyncio.Event()
    while not stop.is_set():
        refill.clear()
        try:
            housekeeping_due = time.monotonic() - last_housekeeping >= HOUSEKEEPING_EVERY_S
            if housekeeping_due:
                await fail_stale_agent_runs()
                await expire_overdue_waiting()
            for cap in caps:
                free = concurrency_for(cap) - len(inflight[cap])
                reserved = free <= effective_interactive_reserve(
                    get_settings().AGENT_INTERACTIVE_RESERVE, concurrency_for(cap))
                for run_id in await claim_agent_runs(cap, free, interactive_only=reserved):
                    t = asyncio.create_task(_guarded(run_id, cfg))
                    inflight[cap].add(t)
                    t.add_done_callback(inflight[cap].discard)
                    # a finished run frees its slot: refill it now, not at the next 1 s tick
                    t.add_done_callback(lambda _t: refill.set())
            if housekeeping_due:
                await fail_unclaimed_runs()     # after the claim: a free slot takes queued work first
                last_housekeeping = time.monotonic()
        except Exception:  # noqa: BLE001 - a bad tick must not kill the lane
            log.exception("agent worker tick failed")
        await _wait_stop_or_wake(stop if wake is None else None, refill, 1.0)
    pending = [t for ts in inflight.values() for t in ts]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    log.info("agent worker stopped")
