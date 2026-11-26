"""Per-project subscription fallback for chat jobs (owner decision 2026-11-26, project vera).

For a project listed in SUBSCRIPTION_FALLBACK (JSON `{"<project id>": "codex-sub/<model>"}`),
a chat-family job walks FREE-tier keys only (a paid API key never runs), and when that walk
gives no answer (no provider available, a spent budget, every answer empty / invalid JSON)
the SAME job row is handed to the subscription worker: it becomes an `agent:codex` run with
the same job id, prompt and response_format, so the client's poll does not change. The
subscription is flat-rate: nothing is booked in usage_log and the project's $ cap is
untouched.

Handoff only happens when the subscription route could have been chosen by the client
itself: the codex route is enabled, the model is VERIFIED, the project may use agent runs
(AGENT_ALLOWED_PROJECT_IDS or the dashboard, plus scope llm:agent), the policy allows the
model, the job has no client tools, and the request is expressible (roles, attachments,
response_format, prompt size). Otherwise the job keeps the normal retry path. The CLI
cannot apply `max_tokens` / `temperature`; the fallback ignores them.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

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

# Lanes the fallback covers: chat completions (chat:deep stays on its own long-context lane).
FALLBACK_CAPABILITIES = frozenset({"chat:fast", "chat:smart", "chat:sales", "chat:code",
                                   "chat:edit", "structured", "prefilter", "translate"})


def fallback_model(project_id: int) -> str | None:
    """The subscription model configured as this project's fallback, or None."""
    raw = get_settings().SUBSCRIPTION_FALLBACK or "{}"
    try:
        table = json.loads(raw)
    except ValueError:
        log.error("SUBSCRIPTION_FALLBACK is not valid JSON: fallback off")
        return None
    model = table.get(str(project_id)) if isinstance(table, dict) else None
    return model if isinstance(model, str) and model else None


def applies(project_id: int, capability: str) -> bool:
    """True when this job walks free keys only and may fall back to the subscription."""
    return capability in FALLBACK_CAPABILITIES and fallback_model(project_id) is not None


def _refusal(project: ProjectRow, req: dict[str, Any], model_id: str) -> str | None:
    """Why the handoff may not happen (None: it may)."""
    s = get_settings()
    spec = subscription_model(model_id)
    if spec is None or spec.provider != "codex-sub":
        return f"{model_id} is not a codex-sub model"
    if not s.AGENT_SUBSCRIPTION_CODEX_ENABLED:
        return "codex subscription route is not enabled"
    if not subscription_model_verified(model_id):
        return f"{model_id} is not verified"
    if AGENT_SCOPE not in (project.allowed_scopes or []) or not project_allowed(project.id):
        return "project may not use agent runs"
    if policy_violation(project.id, model_id.split("/", 1)[1], "owner", PROVIDERS[spec.provider]):
        return "agent policy forbids the model"
    if req.get("tools"):
        return "client tools are not handed off"
    if any(not isinstance(m, dict) or m.get("role") not in SUBSCRIPTION_ROLES
           or m.get("tool_calls") or m.get("tool_call_id") for m in req.get("messages") or []):
        return "tool history is not handed off"
    return response_format_problem(req.get("response_format"))


async def hand_off(row: DeepJobRow, project: ProjectRow, req: dict[str, Any],
                   reason: str) -> bool:
    """Turn the running chat job into a subscription run in place. True when handed off."""
    model_id = fallback_model(project.id)
    if model_id is None:
        return False
    if problem := _refusal(project, req, model_id):
        log.warning("job %d: subscription fallback skipped (%s)", row.id, problem)
        return False
    spec = subscription_model(model_id)
    try:
        messages, attachments = subscription_messages(list(req.get("messages") or []))
    except ValueError as exc:
        log.warning("job %d: subscription fallback skipped (%s)", row.id, exc)
        return False
    if attachments and "vision" not in spec.capabilities:
        log.warning("job %d: subscription fallback skipped (image input, no vision)", row.id)
        return False
    prompt = prompt_for_messages(messages, req.get("response_format"))
    if problem := subscription_prompt_problem(prompt):
        log.warning("job %d: subscription fallback skipped (%s)", row.id, problem)
        return False
    capability = PROVIDERS[spec.provider]
    cli_model = model_id.split("/", 1)[1]
    request = {
        "prompt": prompt, "task_id": "jobs", "account_id": "owner", "session_id": None,
        "wall_timeout_s": 1800, "idle_timeout_s": 300, "allowed_tools": [], "model": cli_model,
        "deadline_s": 21600, "attachments": attachments, "run_mode": "autonomous",
        "accept_paid_continuation_risk": False,
        "client_job": {"capability": row.capability, "provider": spec.provider,
                       "model": model_id, "response_format": req.get("response_format"),
                       "workflow": req.get("workflow"), "tools": [], "tool_choice": None,
                       "terminal_tools": [], "fallback_from": row.capability,
                       "fallback_reason": reason},
    }
    async with get_session() as s:
        moved = await s.execute(
            update(DeepJobRow)
            .where(DeepJobRow.id == row.id, DeepJobRow.status == "running",
                   DeepJobRow.started_at == row.started_at)
            .values(capability=capability, request=request, status="pending",
                    started_at=None, retry_count=0, error_message=None,
                    run_after=datetime.now(UTC).replace(tzinfo=None))
            .execution_options(synchronize_session=False))
        if not moved.rowcount:
            return False          # re-claimed or finished elsewhere: leave it alone
        s.add(AgentRunRow(run_id=row.id, project_id=project.id, capability=capability,
                          task_id="jobs", account_id="owner", model=cli_model))
    log.info("job %d (%s) handed to %s after the free walk failed (%s)",
             row.id, row.capability, model_id, reason)
    await audit(actor=f"project:{project.name}", action="subscription_fallback",
                target=f"job:{row.id}",
                metadata={"from": row.capability, "model": model_id, "reason": reason})
    await wake_worker(capability, row.id)
    return True
