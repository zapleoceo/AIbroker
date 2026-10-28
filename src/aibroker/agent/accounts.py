"""Account selection for NEW subscription runs submitted through /v1/jobs.

Several logins (accounts) can serve one provider, each behind its own worker container
(AGENT_ACCOUNT_ID). A new run goes to the least-loaded account the project policy lists that is
not parked on a usage limit (fewest unfinished runs; ties keep the policy order), so a second
login shares the work instead of idling as a cold spare; when every candidate is parked, to the
one that resets first (the job then waits honestly). The choice is made ONCE at creation: a started run never changes account
(executor._wait_for_quota retries on the same one)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from aibroker.agent.job_status import STATUS_ERROR, error_code
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow, DeepJobRow

_UNFINISHED = ("pending", "queued", "running")

# Bounds the scan of the event log. A session limit resets within hours, but a WEEKLY limit
# (run.failed quota_exhausted / run.waiting_quota with its reset days ahead) must still pause
# the account days later, so the window is longer than the longest reset (7 days) plus slack.
QUOTA_LOOKBACK = timedelta(days=8)

# A failure that says the LOGIN cannot serve at all (not a usage limit): the organization turned off
# subscription access for Claude Code. Every new run on that account fails at once, so selection
# skips it for AGENT_ACCOUNT_DISABLED_PAUSE_S after the failure; the next run after the pause probes
# it again (prod 2026-10-21: 198 runs failed on `owner` while `claude-2` sat idle). A failed OAuth
# refresh counts the same way: every run on that login fails in seconds until it is fixed (prod
# 2026-10-27..28, owner: job 588602 and others).
ACCOUNT_DOWN_CODES = frozenset({"subscription_disabled", "auth_refresh_failed"})


def excluded_accounts() -> frozenset[str]:
    """Accounts the operator took out of new-run selection (AGENT_CLAUDE_EXCLUDED_ACCOUNTS,
    comma-separated; empty = none)."""
    raw = get_settings().AGENT_CLAUDE_EXCLUDED_ACCOUNTS
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _naive_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _reset_of(event_type: str, payload: dict[str, Any],
              ts: datetime | None = None) -> datetime | None:
    """When the pause signalled by this event ends, or None if it pauses nothing: a quota signal
    carries its own reset; an account-down failure at `ts` pauses for AGENT_ACCOUNT_DISABLED_PAUSE_S."""
    if event_type == "run.waiting_quota":
        return _naive_utc(payload.get("not_before"))
    if event_type != "run.failed":
        return None
    if payload.get("error_class") == "quota_exhausted":
        return _naive_utc(payload.get("reset_at"))
    code = error_code(STATUS_ERROR, payload.get("error_class"), payload.get("message"))
    if ts is not None and code in ACCOUNT_DOWN_CODES:
        return ts + timedelta(seconds=get_settings().AGENT_ACCOUNT_DISABLED_PAUSE_S)
    return None


async def quota_resets(accounts: list[str], capability: str,
                       now: datetime | None = None) -> dict[str, datetime]:
    """{account: when its CURRENT pause ends}; accounts without an active pause are absent.
    One query: the run.waiting_quota / run.failed events of the last QUOTA_LOOKBACK for these
    accounts. Every signal whose reset (`_reset_of`: quota reset, or account-down failure + pause)
    is still in the future counts; the account stays paused until the latest of those resets."""
    now = now or _now()
    async with get_session() as s:
        rows = (await s.execute(
            select(AgentRunRow.account_id, AgentRunEventRow.type, AgentRunEventRow.payload,
                   AgentRunEventRow.ts)
            .join(AgentRunEventRow, AgentRunEventRow.run_id == AgentRunRow.run_id)
            .where(AgentRunRow.account_id.in_(accounts), AgentRunRow.capability == capability,
                   AgentRunEventRow.ts >= now - QUOTA_LOOKBACK,
                   AgentRunEventRow.type.in_(("run.waiting_quota", "run.failed")))
        )).all()
    paused: dict[str, datetime] = {}
    for account, event_type, payload, ts in rows:
        reset = _reset_of(event_type, payload or {}, ts)
        if reset is not None and reset > now and (account not in paused or reset > paused[account]):
            paused[account] = reset
    return paused


async def unfinished_runs(accounts: list[str], capability: str) -> dict[str, int]:
    """{account: runs of this capability not finished yet (pending, queued or running)}."""
    async with get_session() as s:
        rows = (await s.execute(
            select(AgentRunRow.account_id, func.count())
            .join(DeepJobRow, DeepJobRow.id == AgentRunRow.run_id)
            .where(AgentRunRow.account_id.in_(accounts), AgentRunRow.capability == capability,
                   DeepJobRow.status.in_(_UNFINISHED))
            .group_by(AgentRunRow.account_id))).all()
    return {account: int(n) for account, n in rows}


async def pick_account(candidates: tuple[str, ...], capability: str,
                       now: datetime | None = None) -> str | None:
    """The account for a NEW run: among candidates without an active quota pause, the one with
    the fewest unfinished runs (ties: policy order); else the candidate whose pause ends first.
    Excluded accounts (`excluded_accounts`) are never candidates. None when none are left."""
    excluded = excluded_accounts()
    candidates = tuple(a for a in candidates if a not in excluded)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    paused = await quota_resets(list(candidates), capability, now)
    free = [a for a in candidates if a not in paused]
    if free:
        load = await unfinished_runs(free, capability)
        return min(free, key=lambda a: (load.get(a, 0), candidates.index(a)))
    return min(candidates, key=lambda a: (paused[a], candidates.index(a)))
