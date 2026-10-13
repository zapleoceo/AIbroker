"""Account selection for NEW subscription runs submitted through /v1/jobs.

Several logins (accounts) can serve one provider, each behind its own worker container
(AGENT_ACCOUNT_ID). A new run goes to the first account the project policy lists that is not
parked on a usage limit; when every candidate is parked, to the one that resets first (the job
then waits honestly). The choice is made ONCE at creation: a started run never changes account
(executor._wait_for_quota retries on the same one)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from aibroker.db import get_session
from aibroker.db.models import AgentRunEventRow, AgentRunRow

# Bounds the scan of the event log. A session limit resets within hours, but a WEEKLY limit
# (run.failed quota_exhausted / run.waiting_quota with its reset days ahead) must still pause
# the account days later, so the window is longer than the longest reset (7 days) plus slack.
QUOTA_LOOKBACK = timedelta(days=8)


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


def _reset_of(event_type: str, payload: dict[str, Any]) -> datetime | None:
    """When the limit signalled by this event ends, or None if it is not a quota signal."""
    if event_type == "run.waiting_quota":
        return _naive_utc(payload.get("not_before"))
    if event_type == "run.failed" and payload.get("error_class") == "quota_exhausted":
        return _naive_utc(payload.get("reset_at"))
    return None


async def quota_resets(accounts: list[str], capability: str,
                       now: datetime | None = None) -> dict[str, datetime]:
    """{account: when its CURRENT quota pause ends}; accounts without an active pause are absent.
    One query: the quota events (run.waiting_quota / run.failed) of the last QUOTA_LOOKBACK for
    these accounts. Every signal whose stored reset time is still in the future counts; the
    account stays paused until the latest of those resets."""
    now = now or _now()
    async with get_session() as s:
        rows = (await s.execute(
            select(AgentRunRow.account_id, AgentRunEventRow.type, AgentRunEventRow.payload)
            .join(AgentRunEventRow, AgentRunEventRow.run_id == AgentRunRow.run_id)
            .where(AgentRunRow.account_id.in_(accounts), AgentRunRow.capability == capability,
                   AgentRunEventRow.ts >= now - QUOTA_LOOKBACK,
                   AgentRunEventRow.type.in_(("run.waiting_quota", "run.failed")))
        )).all()
    paused: dict[str, datetime] = {}
    for account, event_type, payload in rows:
        reset = _reset_of(event_type, payload or {})
        if reset is not None and reset > now and (account not in paused or reset > paused[account]):
            paused[account] = reset
    return paused


async def pick_account(candidates: tuple[str, ...], capability: str,
                       now: datetime | None = None) -> str | None:
    """The account for a NEW run: first candidate (policy order) without an active quota pause,
    else the candidate whose pause ends first. None when there are no candidates."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    paused = await quota_resets(list(candidates), capability, now)
    for account in candidates:
        if account not in paused:
            return account
    return min(candidates, key=lambda a: (paused[a], candidates.index(a)))
