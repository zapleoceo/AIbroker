"""Dashboard re-login sessions: the dashboard opens one, the account's own worker runs it.

Every state change is a compare-and-swap on `state`, so the dashboard and the worker can never
both win a transition (e.g. a cancel racing a code submit). docs/subscription-logins.md.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update

from aibroker.agent.login_seal import seal
from aibroker.db import get_session
from aibroker.db.models import AgentLoginSessionRow

REQUESTED, STARTING, AWAITING = "requested", "starting", "awaiting_user"
CODE_SUBMITTED, VERIFYING = "code_submitted", "verifying"
DONE, FAILED, EXPIRED, CANCELLED = "done", "failed", "expired", "cancelled"
ACTIVE = (REQUESTED, STARTING, AWAITING, CODE_SUBMITTED, VERIFYING)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _swap(session_id: int, expect: tuple[str, ...], **values: object) -> bool:
    values["updated_at"] = _now()
    async with get_session() as s:
        result = await s.execute(
            update(AgentLoginSessionRow)
            .where(AgentLoginSessionRow.id == session_id, AgentLoginSessionRow.state.in_(expect))
            .values(**values))
        return result.rowcount == 1


async def start_session(provider: str, account_id: str) -> int:
    """Open a new re-login, cancelling any unfinished one of the same account first."""
    now = _now()
    async with get_session() as s:
        await s.execute(
            update(AgentLoginSessionRow)
            .where(AgentLoginSessionRow.provider == provider,
                   AgentLoginSessionRow.account_id == account_id,
                   AgentLoginSessionRow.state.in_(ACTIVE))
            .values(state=CANCELLED, code_sealed=None, user_code=None, updated_at=now,
                    message="replaced by a newer sign-in"))
        row = AgentLoginSessionRow(provider=provider, account_id=account_id, state=REQUESTED,
                                   created_at=now, updated_at=now)
        s.add(row)
        await s.flush()
        return int(row.id)


async def latest_session(provider: str, account_id: str) -> AgentLoginSessionRow | None:
    async with get_session() as s:
        return (await s.execute(
            select(AgentLoginSessionRow)
            .where(AgentLoginSessionRow.provider == provider,
                   AgentLoginSessionRow.account_id == account_id)
            .order_by(AgentLoginSessionRow.id.desc()).limit(1))).scalar_one_or_none()


async def get_login(session_id: int) -> AgentLoginSessionRow | None:
    async with get_session() as s:
        return await s.get(AgentLoginSessionRow, session_id)


async def submit_code(session_id: int, code: str) -> str | None:
    """Seal the pasted Claude code to the worker's public key. -> an error text, or None."""
    row = await get_login(session_id)
    if row is None or row.state != AWAITING or not row.public_key:
        return "this sign-in is not waiting for a code any more"
    try:
        sealed = seal(row.public_key, code)
    except ValueError as exc:
        return str(exc)
    if not await _swap(session_id, (AWAITING,), state=CODE_SUBMITTED, code_sealed=sealed,
                       message="code received, the worker is checking it"):
        return "this sign-in is not waiting for a code any more"
    return None


async def cancel_session(session_id: int) -> bool:
    return await _swap(session_id, ACTIVE, state=CANCELLED, code_sealed=None, user_code=None,
                       message="cancelled from the dashboard")


# ─── worker side ────────────────────────────────────────────────────────────


async def claim_requested(provider: str, account_id: str) -> AgentLoginSessionRow | None:
    async with get_session() as s:
        row = (await s.execute(
            select(AgentLoginSessionRow)
            .where(AgentLoginSessionRow.provider == provider,
                   AgentLoginSessionRow.account_id == account_id,
                   AgentLoginSessionRow.state == REQUESTED)
            .order_by(AgentLoginSessionRow.id).limit(1))).scalar_one_or_none()
    if row is None or not await _swap(row.id, (REQUESTED,), state=STARTING,
                                      message="the worker is starting the sign-in"):
        return None
    return row


async def advance(session_id: int, expect: tuple[str, ...], **values: object) -> bool:
    return await _swap(session_id, expect, **values)


async def take_code(session_id: int) -> str | None:
    """The sealed code, removed from the row in the same swap that hands it over."""
    row = await get_login(session_id)
    if row is None or row.state != CODE_SUBMITTED or not row.code_sealed:
        return None
    sealed = row.code_sealed
    async with get_session() as s:
        result = await s.execute(
            update(AgentLoginSessionRow)
            .where(AgentLoginSessionRow.id == session_id,
                   AgentLoginSessionRow.state == CODE_SUBMITTED,
                   AgentLoginSessionRow.code_sealed == sealed)
            .values(state=VERIFYING, code_sealed=None, updated_at=_now()))
    return sealed if result.rowcount == 1 else None


async def finish(session_id: int, state: str, message: str) -> bool:
    return await _swap(session_id, ACTIVE, state=state, message=message[:300],
                       code_sealed=None, user_code=None, public_key=None)


async def expire_orphans(provider: str, account_id: str) -> int:
    """At worker start-up: sessions a previous process was running can no longer finish."""
    async with get_session() as s:
        result = await s.execute(
            update(AgentLoginSessionRow)
            .where(AgentLoginSessionRow.provider == provider,
                   AgentLoginSessionRow.account_id == account_id,
                   AgentLoginSessionRow.state.in_((STARTING, AWAITING, CODE_SUBMITTED, VERIFYING)))
            .values(state=EXPIRED, code_sealed=None, user_code=None, public_key=None,
                    updated_at=_now(), message="the worker restarted; start the sign-in again"))
        return int(result.rowcount or 0)
