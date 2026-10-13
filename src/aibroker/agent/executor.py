"""Drive one claimed agent run to exactly one terminal event.

Order: session lock (resume only) -> workspace -> run.started -> adapter stream with
wall/idle timeouts and a polled cancel flag -> terminal event -> workspace removed.
A failed run is NEVER retried or moved to another account/billing route; a quota
error from the adapter stays `quota_exhausted`.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentCLI, AgentRunSpec
from aibroker.agent.attachments import write_attachments
from aibroker.agent.billing import check_billing_mode
from aibroker.agent.client_tools import clear_pending_call
from aibroker.agent.codex_cli import CodexCLI
from aibroker.agent.constants import project_allowed
from aibroker.agent.lock import Lease, StaleLease, check_fence, session_lock
from aibroker.agent.policy import policy_violation
from aibroker.agent.registry import make_adapter
from aibroker.agent.runs import (
    RunConflict,
    append_event,
    park_for_quota,
    record_usage,
    requeue_run,
)
from aibroker.agent.workspace import cleanup_workspace, collect_files, create_workspace
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.providers.subscriptions import (
    subscription_tool_model_canary,
    subscription_tool_model_verified,
)
from aibroker.services.job_meta import log_tag

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExecConfig:
    workspace_root: str
    cancel_poll_s: float = 1.0
    cancel_grace_s: float = 15.0
    file_max_bytes: int = 256 * 1024
    files_total_bytes: int = 1024 * 1024
    files_max_count: int = 20
    files_ttl_s: int = 3600
    image_max_bytes: int = 2 * 1024 * 1024
    image_max_count: int = 4
    session_busy_delay_s: float = 5.0
    session_busy_max: int = 60
    lease_ttl_s: float = 120.0         # > 3 x heartbeat: a crashed worker's lease expires
    lease_heartbeat_s: float = 30.0
    paid_continuation_verified_off: bool = False
    codex_paid_continuation_verified_off: bool = False
    quota_backoff_base_s: float = 60.0
    quota_backoff_cap_s: float = 1800.0   # exponential backoff cap when no reset time is given
    env: Mapping[str, str] | None = None   # worker env the billing guard inspects (default os.environ)


def exec_config_from_settings() -> ExecConfig:
    s = get_settings()
    return ExecConfig(
        workspace_root=s.AGENT_WORKSPACE_ROOT, cancel_poll_s=s.AGENT_CANCEL_POLL_S,
        cancel_grace_s=s.AGENT_CANCEL_GRACE_S, file_max_bytes=s.AGENT_FILE_MAX_BYTES,
        files_total_bytes=s.AGENT_FILES_TOTAL_BYTES, files_max_count=s.AGENT_FILES_MAX_COUNT,
        files_ttl_s=s.AGENT_FILES_TTL_S, image_max_bytes=s.AGENT_IMAGE_MAX_BYTES,
        image_max_count=s.AGENT_IMAGES_MAX_COUNT, lease_ttl_s=s.AGENT_SESSION_LEASE_TTL_S,
        lease_heartbeat_s=s.AGENT_SESSION_LEASE_HEARTBEAT_S,
        paid_continuation_verified_off=s.AGENT_PAID_CONTINUATION_VERIFIED_OFF,
        codex_paid_continuation_verified_off=s.AGENT_CODEX_PAID_CONTINUATION_VERIFIED_OFF,
    )


async def _finish(run_id: int, terminal: ev.AnyEvent, totals: dict[str, int | None],
                  billing_mode: str | None = None, lease: Lease | None = None) -> str:
    await record_usage(run_id, totals, billing_mode, lease)
    with contextlib.suppress(RunConflict):   # closed under us: the first terminal event wins
        await append_event(run_id, terminal, lease)
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
    if run.capability == "agent:codex" and not get_settings().AGENT_SUBSCRIPTION_CODEX_ENABLED:
        return await _finish(run_id, ev.RunFailed(
            error_class="route_disabled", message="codex subscription route is not enabled"), {})
    req = dict(job.request)
    client_tools = (req.get("client_job") or {}).get("tools") or []
    if client_tools and (not get_settings().AGENT_SUBSCRIPTION_CODEX_TOOLS_ENABLED
                         or not run.model
                         or not (subscription_tool_model_verified(f"codex-sub/{run.model}")
                                 or subscription_tool_model_canary(f"codex-sub/{run.model}",
                                                                   run.project_id))):
        return await _finish(run_id, ev.RunFailed(
            error_class="route_disabled", message="codex client tools are not verified"), {})
    totals: dict[str, int | None] = {}
    if not project_allowed(run.project_id):    # removed from the allowlist while queued
        return await _finish(run_id, ev.RunFailed(
            error_class="project_not_allowed",
            message="project is not in AGENT_ALLOWED_PROJECT_IDS"), totals)
    violation = policy_violation(run.project_id, run.model, run.account_id, run.capability)
    if violation:                              # the policy changed while the run was queued
        return await _finish(run_id, ev.RunFailed(
            error_class=violation, message="outside the project's AGENT_PROJECT_POLICY"), totals)
    if _now() >= job.created_at + timedelta(seconds=float(req.get("deadline_s", 21600))):
        return await _finish(run_id, ev.RunTimedOut(limit="deadline"), totals)
    if run.cancel_requested:
        return await _finish(run_id, ev.RunCancelled(), totals)
    log.info("agent run %d starting: project=%s %s%s", run_id, run.project_id, run.capability,
             log_tag(job.trace_id, job.labels))
    adapter = make_adapter(run.capability)
    if adapter is None:
        return await _finish(run_id, ev.RunFailed(
            error_class="adapter_unavailable", message=f"no adapter for {run.capability}"), totals)
    if run.capability == "agent:codex":
        # CODEX_HOME/generated_images belongs to the account, not a run. One
        # fenced lease across all workers sharing that home prevents overlap.
        async with session_lock("__codex_home__", "generated_images", run_id,
                                ttl_s=cfg.lease_ttl_s,
                                heartbeat_s=cfg.lease_heartbeat_s) as image_lease:
            if image_lease is None:
                await requeue_run(run_id, cfg.session_busy_delay_s)
                return None
            return await _execute_with_session(run, job, adapter, cfg, image_lease)
    return await _execute_with_session(run, job, adapter, cfg, None)


async def _execute_with_session(run: AgentRunRow, job: DeepJobRow, adapter: AgentCLI,
                                cfg: ExecConfig, image_lease: Lease | None) -> str | None:
    resume = run.resume_session_id
    if not resume:
        return await _held_drive(run, job, adapter, cfg, image_lease)
    async with session_lock(run.account_id, resume, run.run_id, ttl_s=cfg.lease_ttl_s,
                            heartbeat_s=cfg.lease_heartbeat_s) as lease:
        if lease is None:
            if await requeue_run(run.run_id, cfg.session_busy_delay_s) > cfg.session_busy_max:
                return await _finish(run.run_id, ev.RunFailed(
                    error_class="session_busy", message="session stayed locked"), {})
            return None
        return await _held_drive(run, job, adapter, cfg, lease, image_lease)


async def _held_drive(run: AgentRunRow, job: DeepJobRow, adapter: AgentCLI, cfg: ExecConfig,
                      lease: Lease | None, image_lease: Lease | None = None) -> str | None:
    """_drive, but a run whose lease was taken over (StaleLease on a fenced write) simply
    stops: the newer holder owns the session and this zombie writes nothing more."""
    try:
        return await _drive(run, job, adapter, cfg, lease, image_lease)
    except StaleLease:
        log.error("run %d lost its session lease; stopping without further writes", run.run_id)
        return None


async def _check_model(adapter: AgentCLI, model: str | None) -> ev.RunFailed | None:
    """An explicit model id is passed through EXACTLY or the run fails - never swapped
    for another/latest. The project's model allowlist (policy_violation) is MANDATORY and has
    already been enforced; this CLI-catalog check is additional, when the catalog is readable.
    `latest` is honoured only because the caller wrote it AND the policy lists it."""
    if not model or model == "latest":
        return None
    try:
        catalog = await adapter.list_models()
    except Exception:  # noqa: BLE001 - catalog unreadable: the (already enforced) allowlist decides
        return None
    if catalog is not None and model not in {m.id for m in catalog}:
        return ev.RunFailed(error_class="model_unavailable",
                            message=f"model {model!r} is not in the CLI catalog")
    return None


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _wait_for_quota(run: AgentRunRow, job: DeepJobRow, failed: ev.RunFailed,
                          totals: dict[str, int | None], mode: str, cfg: ExecConfig,
                          lease: Lease | None = None) -> str:
    """Quota exhausted = WAIT: requeue until the reported reset time (+5 s), else
    exponential backoff capped at quota_backoff_cap_s. Same account, same subscription
    route - never API/OpenRouter, never another account. Past the run's overall
    deadline it ends `run.failed` (quota_exhausted) carrying the CLI's wording and reset time -
    still no fallback, no retry on another account."""
    now = _now()
    reset = None
    if failed.reset_at:
        with contextlib.suppress(ValueError):
            reset = datetime.fromisoformat(failed.reset_at).astimezone(UTC).replace(tzinfo=None)
    delay = max(0.0, (reset - now).total_seconds()) + 5.0 if reset else min(
        cfg.quota_backoff_base_s * 2 ** run.quota_waits, cfg.quota_backoff_cap_s)
    not_before = now + timedelta(seconds=delay)
    deadline = job.created_at + timedelta(seconds=float(job.request.get("deadline_s", 21600)))
    if not_before >= deadline:
        when = f"reset {failed.reset_at}" if reset else f"next retry {not_before.isoformat()}Z"
        return await _finish(run.run_id, ev.RunFailed(
            error_class="quota_exhausted", reset_at=failed.reset_at,
            message=f"{failed.message} ({when} is past the job deadline)"), totals, mode, lease)
    await record_usage(run.run_id, totals, mode, lease)
    await park_for_quota(run.run_id, not_before, reset_reported=reset is not None, lease=lease,
                         reason=failed.message[:300])
    return "waiting_quota"


async def _drive(run: AgentRunRow, job: DeepJobRow, adapter: AgentCLI,
                 cfg: ExecConfig, lease: Lease | None = None,
                 image_lease: Lease | None = None) -> str:
    run_id = run.run_id
    req = dict(job.request)
    totals: dict[str, int | None] = {}
    workspace = create_workspace(cfg.workspace_root, run_id)
    try:
        async with get_session() as s:
            await check_fence(s, lease)          # a stale holder writes nothing, not even this
            row = await s.get(AgentRunRow, run_id)
            if row is not None:
                row.workspace = workspace
        await append_event(run_id, ev.RunStarted(
            capability=run.capability, account_id=run.account_id,
            resumed_session_id=run.resume_session_id), lease)
        if run.resume_session_id and not adapter.supports_resume:
            return await _finish(run_id, ev.RunFailed(error_class="resume_unsupported"), totals,
                                 "unverified", lease)
        verified_off = (cfg.codex_paid_continuation_verified_off
                        if run.capability == "agent:codex"
                        else cfg.paid_continuation_verified_off)
        mode, refusal = await check_billing_mode(
            run.capability, adapter, os.environ if cfg.env is None else cfg.env,
            run_mode=str(req.get("run_mode", "autonomous")),
            risk_ack=bool(req.get("accept_paid_continuation_risk", False)),
            verified_off=verified_off)
        auth = adapter.attestation() if refusal is None else None
        if refusal is None and auth is None:
            refusal = ev.RunFailed(error_class="billing_mode_refused",
                                   message="no auth attestation from the CLI probe")
        if refusal is not None:   # fail closed, before the CLI is ever started
            return await _finish(run_id, refusal, totals, mode, lease)
        async with get_session() as s:
            project = await s.get(ProjectRow, run.project_id)
        await append_event(run_id, ev.BillingAttested(
            auth_method=auth.auth_method, api_provider=auth.api_provider,
            paid_continuation_verified_off=verified_off,
            fallback="none",       # this path never falls back to API billing; recorded per run
            account_id=run.account_id,
            project_daily_cap_usd=(None if project is None or project.daily_cost_cap_usd is None
                                   else float(project.daily_cost_cap_usd))), lease)
        bad_model = await _check_model(adapter, run.model)
        if bad_model is not None:
            return await _finish(run_id, bad_model, totals, mode, lease)
        try:
            attached = write_attachments(workspace, list(req.get("attachments") or []))
        except ValueError as exc:
            return await _finish(run_id, ev.RunFailed(error_class="attachment_invalid",
                                                      message=str(exc)[:500]), totals, mode, lease)
        spec = AgentRunSpec(
            run_id=run_id, capability=run.capability, prompt=req["prompt"],
            workspace=workspace, account_id=run.account_id, session_id=run.resume_session_id,
            allowed_tools=list(req.get("allowed_tools") or []), model=run.model,
            attachments=attached,
            client_tools=list((req.get("client_job") or {}).get("tools") or []),
            tool_choice=(req.get("client_job") or {}).get("tool_choice"),
            fence_leases=tuple(held for held in (lease, image_lease) if held is not None))
        if isinstance(adapter, CodexCLI):
            adapter.snapshot_artifacts()
        # 1 s of slack: file mtimes come from the kernel's coarse clock and can lag time.time()
        started_at = time.time() - 1.0
        terminal, changed = await _stream(
            run_id, adapter, spec, totals, float(req.get("wall_timeout_s", 1800)),
            float(req.get("wall_timeout_s", 1800) if spec.client_tools else
                  req.get("idle_timeout_s", 300)), cfg, lease, image_lease)
        if isinstance(terminal, ev.RunFailed) and terminal.error_class == "quota_exhausted":
            return await _wait_for_quota(run, job, terminal, totals, mode, cfg, lease)
        if isinstance(terminal, ev.RunCompleted):
            if image_lease is not None:
                async with get_session() as s:
                    await check_fence(s, image_lease)
            paths = changed + [f.path for f in terminal.files]
            files = await asyncio.to_thread(
                collect_files, workspace, paths, max_bytes=cfg.file_max_bytes,
                total_bytes=cfg.files_total_bytes, max_count=cfg.files_max_count,
                ttl_s=cfg.files_ttl_s)
            images = await asyncio.to_thread(_export_images, adapter, spec, started_at, cfg)
            if image_lease is not None:
                async with get_session() as s:
                    await check_fence(s, image_lease)
            terminal = ev.RunCompleted(final_text=terminal.final_text, files=[*files, *images])
        return await _finish(run_id, terminal, totals, mode, lease)
    finally:
        cleanup_workspace(workspace)


def _export_images(adapter: AgentCLI, spec: AgentRunSpec, since: float,
                   cfg: ExecConfig) -> list[ev.FileRef]:
    """The worker collects the CLI's out-of-workspace artifacts (Codex: generated PNGs)."""
    for attr, value in (("image_max_bytes", cfg.image_max_bytes), ("image_max_count", cfg.image_max_count),
                        ("image_ttl_s", cfg.files_ttl_s)):
        if hasattr(adapter, attr):
            setattr(adapter, attr, value)
    try:
        return adapter.export_artifacts(spec, since)
    except Exception:  # noqa: BLE001 - a missing image must never fail an otherwise finished run
        log.exception("artifact export failed for run %d", spec.run_id)
        return []


async def _stream(run_id: int, adapter: AgentCLI, spec: AgentRunSpec,
                  totals: dict[str, int | None], wall_s: float, idle_s: float,
                  cfg: ExecConfig, lease: Lease | None = None,
                  image_lease: Lease | None = None) -> tuple[ev.AnyEvent, list[str]]:
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
    lease_lost = False
    try:
        while terminal is None:
            now = time.monotonic()
            if any(held is not None and held.lost.is_set() for held in (lease, image_lease)):
                lease_lost = True       # stop the CLI at once, like a cancel, but write nothing
                break
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
            if isinstance(item, ev.RunCompleted | ev.RunFailed | ev.RunCancelled):
                terminal = item
                break
            if isinstance(item, ev.Usage):
                for key in ("input_tokens", "output_tokens", "total_tokens",
                            "cache_read_tokens", "reasoning_tokens", "turns"):
                    val = getattr(item, key)
                    if val is not None:
                        totals[key] = (totals.get(key) or 0) + val
            elif isinstance(item, ev.FileChanged) and item.action != "deleted":
                changed.append(item.path)
            await append_event(run_id, item, lease)
            if isinstance(item, ev.Usage):
                log.info(
                    "agent_usage job_id=%d run_id=%d source=%s input_tokens=%s output_tokens=%s "
                    "total_tokens=%s cache_read_tokens=%s reasoning_tokens=%s cost_usd=unknown",
                    run_id, run_id, item.source, item.input_tokens, item.output_tokens,
                    item.total_tokens, item.cache_read_tokens, item.reasoning_tokens,
                )
    finally:
        if terminal is None or isinstance(terminal, ev.RunCancelled | ev.RunTimedOut):
            await _stop_adapter(adapter, cfg.cancel_grace_s)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        if lease_lost:
            # Unfenced on purpose: it only withdraws this run's own unanswered tool call, and a
            # lost lease means no terminal event will ever do it.
            try:
                await clear_pending_call(run_id)
            except Exception:
                log.exception("could not withdraw the pending client tool of run %d", run_id)
    if lease_lost:
        raise StaleLease(f"run {run_id} can no longer assume its session lease")
    return terminal or ev.RunFailed(error_class="no_terminal_event"), changed
