"""Per-project subscription-first routing for chat jobs (owner decision 2026-11-26, project vera).

SUBSCRIPTION_PRIMARY (JSON) names, per project, a codex-sub model and the chat lanes it
serves FIRST, e.g. `{"2": {"model": "codex-sub/gpt-6.1-sol", "capabilities": ["chat:smart"]}}`.

Flow for a matching job (same job id throughout, the client's poll never changes):

1. The LLM dispatcher claims the chat job and, before any API key is touched, `hand_off`
   turns the row into an `agent:codex` run in place (same prompt and response_format). The
   original chat request is kept in `client_job.fallback_request`.
2. The subscription answers: the job finishes as a normal subscription job (flat-rate,
   nothing in usage_log, the project's $ cap untouched).
3. The subscription fails or times out (quota, 429, CLI / worker error, invalid JSON):
   `revert_to_api` (called from agent/runs.py) puts the original chat request back,
   marks it `subscription_tried`, and re-queues the job; the dispatcher then runs the
   project's NORMAL API walk for that lane, unchanged (free keys first, then the paid tail
   as configured). A cancelled run stays cancelled.

Handoff only happens when the client could have chosen the subscription itself: codex route
enabled, model VERIFIED, project allowed for agent runs (AGENT_ALLOWED_PROJECT_IDS or the
dashboard) AND scope llm:agent, policy allows the model, no client tools, expressible request.
A request that sets `key_tier` explicitly keeps its API path. The CLI cannot apply
`max_tokens` / `temperature`; the subscription leg ignores them. The run never gets a shell
(`no_shell`): triaged chat text is third-party input.
"""
from __future__ import annotations

import functools
import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text, update

from aibroker.agent.constants import AGENT_SCOPE, project_allowed
from aibroker.agent.policy import policy_violation
from aibroker.agent.rpc import wake_worker
from aibroker.agent.subscription_input import subscription_messages
from aibroker.agent.subscription_job import prompt_for_messages, response_format_problem
from aibroker.config import get_settings
from aibroker.db.engine import get_session
from aibroker.db.models import AgentRunRow, DeepJobRow, ProjectRow
from aibroker.providers.job_contract import SUBSCRIPTION_ROLES, subscription_prompt_problem
from aibroker.providers.subscriptions import (
    PROVIDERS,
    subscription_model,
    subscription_model_verified,
)
from aibroker.telemetry import audit

log = logging.getLogger(__name__)

PRIMARY_WALL_S = 180
PRIMARY_IDLE_S = 120
PRIMARY_DEADLINE_S = 300
# Per-project share of the shared codex lane and how long a handed-off job may wait for a
# worker slot before it falls back to the API walk (both overridable per project entry).
DEFAULT_MAX_CONCURRENT = 2
DEFAULT_QUEUE_TIMEOUT_S = 20
_LOCK_NAMESPACE = 0x5B5C   # two-int advisory lock: (namespace, project id)

# Lanes a subscription may serve first (chat:deep stays on its long-context lane).
PRIMARY_CAPABILITIES = frozenset({"chat:fast", "chat:smart", "chat:sales", "chat:code",
                                  "chat:edit", "structured", "prefilter", "translate"})


@functools.lru_cache(maxsize=4)
def _parse(raw: str) -> dict[str, Any]:
    """Parsed SUBSCRIPTION_PRIMARY, cached per raw value (a bad value logs once)."""
    try:
        table = json.loads(raw)
    except ValueError:
        log.error("SUBSCRIPTION_PRIMARY is not valid JSON: subscription-first routing off")
        return {}
    return table if isinstance(table, dict) else {}


def _table() -> dict[str, Any]:
    return _parse(get_settings().SUBSCRIPTION_PRIMARY or "{}")


def _configured_projects() -> list[int]:
    return [int(k) for k in _table() if str(k).isdigit()]


def _entry(project_id: int) -> dict[str, Any] | None:
    entry = _table().get(str(project_id))
    if (not isinstance(entry, dict) or not isinstance(entry.get("model"), str)
            or not isinstance(entry.get("capabilities"), list)):
        return None
    return entry


def _int_setting(entry: dict[str, Any], key: str, default: int) -> int:
    value = entry.get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 \
        else default


def max_concurrent(project_id: int) -> int:
    """How many of this project's subscription-first runs may be in the codex lane at once."""
    entry = _entry(project_id)
    return _int_setting(entry, "max_concurrent", DEFAULT_MAX_CONCURRENT) if entry \
        else DEFAULT_MAX_CONCURRENT


def queue_timeout_s(project_id: int) -> int:
    """How long a handed-off job may wait for a codex worker slot before the API fallback."""
    entry = _entry(project_id)
    return _int_setting(entry, "queue_timeout_s", DEFAULT_QUEUE_TIMEOUT_S) if entry \
        else DEFAULT_QUEUE_TIMEOUT_S


def primary_model(project_id: int, capability: str) -> str | None:
    """The subscription model that serves `capability` first for this project, or None."""
    entry = _entry(project_id)
    if entry is None:
        return None
    lanes = entry["capabilities"]
    return entry["model"] if capability in PRIMARY_CAPABILITIES and capability in lanes else None


def enabled() -> bool:
    """Any project configured at all (lets the dispatcher skip the sweep without a query)."""
    return bool(_table())


async def _in_flight(s: Any, project_id: int) -> int:
    """This project's subscription-first runs queued or running in the codex lane."""
    rows = (await s.execute(
        select(DeepJobRow.request["client_job"]["subscription_first"])
        .where(DeepJobRow.project_id == project_id, DeepJobRow.capability == "agent:codex",
               DeepJobRow.status.in_(("pending", "running"))))).all()
    return sum(1 for (first,) in rows if first in (True, "true"))


def wants_subscription(project_id: int, capability: str, req: dict[str, Any]) -> bool:
    """True when this claimed chat job should go to the subscription before any API key.
    A job that pins an API `model` or sets `key_tier` keeps its own API path."""
    return (not req.get("subscription_tried") and not req.get("key_tier")
            and not req.get("model") and primary_model(project_id, capability) is not None)


def _refusal(project: ProjectRow, req: dict[str, Any], model_id: str) -> str | None:
    """Why the handoff may not happen (None: it may)."""
    spec = subscription_model(model_id)
    if spec is None or spec.provider != "codex-sub":
        return f"{model_id} is not a codex-sub model"
    if not get_settings().AGENT_SUBSCRIPTION_CODEX_ENABLED:
        return "codex subscription route is not enabled"
    if not subscription_model_verified(model_id):
        return f"{model_id} is not verified"
    if AGENT_SCOPE not in (project.allowed_scopes or []) or not project_allowed(project.id):
        return "project may not use agent runs"
    if policy_violation(project.id, model_id.split("/", 1)[1], "owner", PROVIDERS[spec.provider]):
        return "agent policy forbids the model"
    if req.get("tools"):
        return "client tools stay on the API path"
    if any(not isinstance(m, dict) or m.get("role") not in SUBSCRIPTION_ROLES
           or m.get("tool_calls") or m.get("tool_call_id") for m in req.get("messages") or []):
        return "tool history stays on the API path"
    return response_format_problem(req.get("response_format"))


async def hand_off(row: DeepJobRow, project: ProjectRow, req: dict[str, Any]) -> bool:
    """Turn the claimed chat job into a subscription run in place. True when handed off;
    False leaves the job to the normal API walk (a warning says why)."""
    model_id = primary_model(project.id, row.capability)
    if model_id is None:
        return False
    if problem := _refusal(project, req, model_id):
        log.warning("job %d: subscription-first skipped (%s)", row.id, problem)
        return False
    spec = subscription_model(model_id)
    try:
        messages, attachments = subscription_messages(list(req.get("messages") or []))
    except ValueError as exc:
        log.warning("job %d: subscription-first skipped (%s)", row.id, exc)
        return False
    if attachments and "vision" not in spec.capabilities:
        log.warning("job %d: subscription-first skipped (image input, no vision)", row.id)
        return False
    prompt = prompt_for_messages(messages, req.get("response_format"))
    if problem := subscription_prompt_problem(prompt):
        log.warning("job %d: subscription-first skipped (%s)", row.id, problem)
        return False
    capability = PROVIDERS[spec.provider]
    cli_model = model_id.split("/", 1)[1]
    request = {
        # Short limits: a slow or queued subscription must fall back to the API walk quickly
        # (deadline counts from job creation, so a worker backlog also ends in the fallback).
        "prompt": prompt, "task_id": "jobs", "account_id": "owner", "session_id": None,
        "wall_timeout_s": PRIMARY_WALL_S, "idle_timeout_s": PRIMARY_IDLE_S, "allowed_tools": [],
        "model": cli_model, "deadline_s": PRIMARY_DEADLINE_S, "attachments": attachments,
        "run_mode": "autonomous",
        "accept_paid_continuation_risk": False,
        "client_job": {"capability": row.capability, "provider": spec.provider,
                       "model": model_id, "response_format": req.get("response_format"),
                       "workflow": req.get("workflow"), "tools": [], "tool_choice": None,
                       "terminal_tools": [], "no_shell": True, "subscription_first": True,
                       "fallback_capability": row.capability, "fallback_request": req},
    }
    cap = max_concurrent(project.id)
    async with get_session() as s:
        if s.bind.dialect.name == "postgresql":   # pragma: no cover — Postgres-only lock
            # One handoff per project at a time, so concurrent dispatcher tasks cannot
            # overshoot the cap (released at commit).
            await s.execute(text("SELECT pg_advisory_xact_lock(:ns, :k)"),
                            {"ns": _LOCK_NAMESPACE, "k": project.id})
        if await _in_flight(s, project.id) >= cap:
            log.info("job %d: %s lane share full (%d) - straight to the API walk",
                     row.id, model_id, cap)
            return False
        moved = await s.execute(
            update(DeepJobRow)
            .where(DeepJobRow.id == row.id, DeepJobRow.status == "running",
                   DeepJobRow.started_at == row.started_at,
                   DeepJobRow.cancel_requested.is_(False))   # a cancel stays a cancel
            .values(capability=capability, request=request, status="pending",
                    started_at=None, retry_count=0, error_message=None,
                    run_after=datetime.now(UTC).replace(tzinfo=None))
            .execution_options(synchronize_session=False))
        if not moved.rowcount:
            return False          # re-claimed, finished or cancelled: the API path handles it
        s.add(AgentRunRow(run_id=row.id, project_id=project.id, capability=capability,
                          task_id="jobs", account_id="owner", model=cli_model))
    log.info("job %d (%s) sent to %s first", row.id, row.capability, model_id)
    await audit(actor=f"project:{project.name}", action="subscription_first",
                target=f"job:{row.id}", metadata={"lane": row.capability, "model": model_id})
    await wake_worker(capability, row.id)
    return True


async def expire_overdue(now: datetime | None = None) -> int:
    """Time out subscription-first runs still PENDING (no codex worker slot yet) past their
    project's queue timeout since the handoff, or past the overall deadline since the job was
    created. The executor checks deadlines only at claim, so a full or dead codex lane would
    otherwise keep them; this runs in the LLM dispatcher every few seconds, works with no agent
    worker alive, and each timeout reverts the job to the API walk through the normal terminal
    path. Returns how many were timed out."""
    from aibroker.agent.events import RunTimedOut
    from aibroker.agent.runs import RunConflict, append_event

    if not enabled():
        return 0
    now = now or datetime.now(UTC).replace(tzinfo=None)
    async with get_session() as s:
        rows = (await s.execute(
            select(DeepJobRow.id, DeepJobRow.project_id, DeepJobRow.created_at,
                   DeepJobRow.run_after, DeepJobRow.request["client_job"]["subscription_first"])
            .where(DeepJobRow.capability == "agent:codex", DeepJobRow.status == "pending",
                   DeepJobRow.project_id.in_(_configured_projects())))).all()
    expired = 0
    for job_id, project_id, created_at, handed_at, first in rows:
        if first not in (True, "true"):
            continue
        waited_out = handed_at is not None and (
            now - handed_at).total_seconds() > queue_timeout_s(project_id)
        overdue = (now - created_at).total_seconds() > PRIMARY_DEADLINE_S
        if not (waited_out or overdue):
            continue
        try:
            await append_event(job_id, RunTimedOut(limit="deadline"), only_if_pending=True)
            expired += 1
        except RunConflict:
            continue                     # claimed or closed meanwhile
    if expired:
        log.warning("subscription-first: %d pending run(s) got no codex slot in time - back to "
                    "the API walk", expired)
    return expired


def without_fallback_request(job: DeepJobRow, client_job: dict[str, Any]) -> None:
    """Drop the kept original request once a subscription-first run ends for good (it would
    otherwise double the row for the whole retention)."""
    if "fallback_request" in client_job and isinstance(job.request, dict):
        job.request = {**job.request,
                       "client_job": {k: v for k, v in client_job.items()
                                      if k != "fallback_request"}}


def revert_to_api(job: DeepJobRow, client_job: dict[str, Any], outcome: str,
                  error: str | None, now: datetime) -> bool:
    """Called inside agent/runs.py's terminal-event transaction for a failed / timed-out
    subscription-first run: restore the original chat job and re-queue it for the API walk.
    False when this job is not a subscription-first job (the run then ends as usual)."""
    original = client_job.get("fallback_request")
    lane = client_job.get("fallback_capability")
    if (not client_job.get("subscription_first") or not isinstance(original, dict) or not lane
            or job.cancel_requested):
        return False                  # a cancel stays a cancel: the run ends as usual
    job.capability = lane
    job.request = {**original, "subscription_tried": {"model": client_job.get("model"),
                                                     "outcome": outcome,
                                                     "error": (error or "")[:300]}}
    job.status = "pending"
    job.started_at = None
    job.completed_at = None
    job.retry_count = 0
    job.error_message = None
    job.run_after = now
    log.warning("job %d: subscription %s %s (%s) - back to the %s API walk", job.id,
                client_job.get("model"), outcome, (error or "")[:120], lane)
    return True
