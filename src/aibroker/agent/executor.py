"""Drive one claimed agent run to exactly one terminal event.

Order: session lock (resume only) -> workspace -> run.started -> adapter stream with
wall/idle timeouts and a polled cancel flag -> terminal event -> workspace removed.
A failed run is NEVER retried or moved to another account/billing route; a quota
error from the adapter stays `quota_exhausted`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentCLI, AgentRunSpec
from aibroker.agent.billing import check_billing_mode
from aibroker.agent.lock import session_lock
from aibroker.agent.registry import make_adapter
from aibroker.agent.runs import RunConflict, append_event, record_usage, requeue_run
from aibroker.agent.workspace import cleanup_workspace, collect_files, create_workspace
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow


@dataclass(frozen=True)
class ExecConfig:
    workspace_root: str
    cancel_poll_s: float = 1.0
    cancel_grace_s: float = 15.0
    file_max_bytes: int = 256 * 1024
    files_total_bytes: int = 1024 * 1024
    files_max_count: int = 20
    files_ttl_s: int = 3600
    session_busy_delay_s: float = 5.0
    session_busy_max: int = 60
    env: Mapping[str, str] | None = None   # worker env the billing guard inspects (default os.environ)


def exec_config_from_settings() -> ExecConfig:
    s = get_settings()
    return ExecConfig(
        workspace_root=s.AGENT_WORKSPACE_ROOT, cancel_poll_s=s.AGENT_CANCEL_POLL_S,
        cancel_grace_s=s.AGENT_CANCEL_GRACE_S, file_max_bytes=s.AGENT_FILE_MAX_BYTES,
        files_total_bytes=s.AGENT_FILES_TOTAL_BYTES, files_max_count=s.AGENT_FILES_MAX_COUNT,
        files_ttl_s=s.AGENT_FILES_TTL_S,
    )


@contextlib.asynccontextmanager
async def _no_lock() -> AsyncIterator[bool]:
    yield True


async def _finish(run_id: int, terminal: ev.AnyEvent, totals: dict[str, int | None],
                  billing_mode: str | None = None) -> str:
    await record_usage(run_id, totals, billing_mode)
    with contextlib.suppress(RunConflict):   # closed under us: the first terminal event wins
        await append_event(run_id, terminal)
    return ev.OUTCOME_BY_TYPE[terminal.type]


async def _cancel_flag(run_id: int) -> bool:
    async with get_session() as s:
        run = await s.get(AgentRunRow, run_id)
        return bool(run and run.cancel_requested)


async def _stop_adapter(adapter: AgentCLI, grace_s: float) -> None:
    """adapter.cancel() owns SIGTERM -> SIGKILL; we only bound how long we wait for it."""
    with contextlib.suppress(Exception):
        await asyncio.wait_for(adapter.cancel(), timeout=grace_s)


async def execute_run(run_id: int, cfg: ExecConfig) -> str | None:
    """Run one claimed run -> its outcome, or None when it was put back in the queue
    (resume session busy)."""
    async with get_session() as s:
        run = await s.get(AgentRunRow, run_id)
        job = await s.get(DeepJobRow, run_id)
    if run is None or job is None:
        return None
    if run.outcome is not None:
        return run.outcome
    req = dict(job.request)
    totals: dict[str, int | None] = {}
    if run.cancel_requested:
        return await _finish(run_id, ev.RunCancelled(), totals)
    adapter = make_adapter(run.capability)
    if adapter is None:
        return await _finish(run_id, ev.RunFailed(
            error_class="adapter_unavailable", message=f"no adapter for {run.capability}"), totals)
    resume = run.resume_session_id
    async with (session_lock(run.account_id, resume) if resume else _no_lock()) as got:
        if not got:
            if await requeue_run(run_id, cfg.session_busy_delay_s) > cfg.session_busy_max:
                return await _finish(run_id, ev.RunFailed(
                    error_class="session_busy", message="session stayed locked"), totals)
            return None
        return await _drive(run, req, adapter, cfg)


async def _check_model(adapter: AgentCLI, model: str | None) -> ev.RunFailed | None:
    """An explicit model id is passed through EXACTLY or the run fails - never swapped
    for another/latest. `latest` is honoured only because the caller wrote it."""
    if not model or model == "latest":
        return None
    try:
        catalog = await adapter.list_models()
    except Exception:  # noqa: BLE001 - catalog unreadable: pass the id through unchanged
        return None
    if catalog is not None and model not in {m.id for m in catalog}:
        return ev.RunFailed(error_class="model_unavailable",
                            message=f"model {model!r} is not in the CLI catalog")
    return None


async def _drive(run: AgentRunRow, req: dict[str, Any], adapter: AgentCLI,
                 cfg: ExecConfig) -> str:
    run_id = run.run_id
    totals: dict[str, int | None] = {}
    workspace = create_workspace(cfg.workspace_root, run_id)
    try:
        async with get_session() as s:
            row = await s.get(AgentRunRow, run_id)
            if row is not None:
                row.workspace = workspace
        await append_event(run_id, ev.RunStarted(
            capability=run.capability, account_id=run.account_id,
            resumed_session_id=run.resume_session_id))
        if run.resume_session_id and not adapter.supports_resume:
            return await _finish(run_id, ev.RunFailed(error_class="resume_unsupported"), totals,
                                 "unverified")
        mode, refusal = await check_billing_mode(
            run.capability, adapter, os.environ if cfg.env is None else cfg.env)
        if refusal is not None:   # fail closed, before the CLI is ever started
            return await _finish(run_id, refusal, totals, mode)
        bad_model = await _check_model(adapter, run.model)
        if bad_model is not None:
            return await _finish(run_id, bad_model, totals, mode)
        spec = AgentRunSpec(
            run_id=run_id, capability=run.capability, prompt=req["prompt"],
            workspace=workspace, account_id=run.account_id, session_id=run.resume_session_id,
            allowed_tools=list(req.get("allowed_tools") or []), model=run.model)
        terminal, changed = await _stream(
            run_id, adapter, spec, totals, float(req.get("wall_timeout_s", 1800)),
            float(req.get("idle_timeout_s", 300)), cfg)
        if isinstance(terminal, ev.RunCompleted):
            paths = changed + [f.path for f in terminal.files]
            files = await asyncio.to_thread(
                collect_files, workspace, paths, max_bytes=cfg.file_max_bytes,
                total_bytes=cfg.files_total_bytes, max_count=cfg.files_max_count,
                ttl_s=cfg.files_ttl_s)
            terminal = ev.RunCompleted(final_text=terminal.final_text, files=files)
        return await _finish(run_id, terminal, totals, mode)
    finally:
        cleanup_workspace(workspace)


async def _stream(run_id: int, adapter: AgentCLI, spec: AgentRunSpec,
                  totals: dict[str, int | None], wall_s: float, idle_s: float,
                  cfg: ExecConfig) -> tuple[ev.AnyEvent, list[str]]:
    q: asyncio.Queue[ev.AnyEvent | None] = asyncio.Queue()
    changed: list[str] = []

    async def pump() -> None:
        try:
            async for item in adapter.start(spec):
                await q.put(item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any adapter crash is a failed run
            await q.put(ev.RunFailed(error_class="adapter_error", message=str(exc)[:500]))
        await q.put(None)

    task = asyncio.create_task(pump())
    t0 = last = last_poll = time.monotonic()
    terminal: ev.AnyEvent | None = None
    try:
        while terminal is None:
            now = time.monotonic()
            if now - t0 >= wall_s:
                terminal = ev.RunTimedOut(limit="wall")
            elif now - last >= idle_s:
                terminal = ev.RunTimedOut(limit="idle")
            elif now - last_poll >= cfg.cancel_poll_s:
                last_poll = now
                if await _cancel_flag(run_id):
                    terminal = ev.RunCancelled()
            if terminal is not None:
                break
            wait = min(cfg.cancel_poll_s, wall_s - (now - t0), idle_s - (now - last))
            try:
                item = await asyncio.wait_for(q.get(), timeout=max(wait, 0.001))
            except TimeoutError:
                continue
            if item is None:
                break
            last = time.monotonic()
            if isinstance(item, ev.RunCompleted | ev.RunFailed):
                terminal = item
                break
            if isinstance(item, ev.Usage):
                for key in ("input_tokens", "output_tokens", "cache_read_tokens", "turns"):
                    val = getattr(item, key)
                    if val is not None:
                        totals[key] = (totals.get(key) or 0) + val
            elif isinstance(item, ev.FileChanged) and item.action != "deleted":
                changed.append(item.path)
            await append_event(run_id, item)
    finally:
        if terminal is None or isinstance(terminal, ev.RunCancelled | ev.RunTimedOut):
            await _stop_adapter(adapter, cfg.cancel_grace_s)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return terminal or ev.RunFailed(error_class="no_terminal_event"), changed
