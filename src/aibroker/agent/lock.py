"""Exclusive per-(account_id, session_id) lease with fencing: two runs never drive one CLI session.

A lease ROW, not a database session lock, so the worker needs nothing but the pgbouncer
(transaction pooling) connection: no direct Postgres connection, no advisory lock, no LISTEN,
no session-level state of any kind.

- acquire: one atomic `INSERT ... ON CONFLICT DO UPDATE ... WHERE lease_until < now` - it
  succeeds only if the session is free or its lease has EXPIRED, and every successful
  acquisition bumps the row's FENCE token (monotonic per session);
- heartbeat: while the run is alive a background task extends `lease_until` every
  `heartbeat_s`; the TTL is several heartbeats long, so a crashed worker's lease expires;
- fencing: a run's writes (events, status, result, usage) call `check_fence` in their own
  transaction, which locks the lease row and rejects (`StaleLease`) a holder whose lease was
  taken over - a zombie worker can never write after its successor started;
- release: the row is expired in place (never deleted), so the fence counter survives and a
  later holder always gets a larger token.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db import get_session
from aibroker.db.models import AgentSessionLeaseRow

log = logging.getLogger(__name__)

DEFAULT_TTL_S = 120.0
DEFAULT_HEARTBEAT_S = 30.0
_RELEASED = timedelta(days=1)       # lease_until of a released row: long expired


class StaleLease(Exception):
    """This run no longer holds its session lease (taken over): it must not write."""


@dataclass(frozen=True)
class Lease:
    """A held lease and its fence token."""
    account_id: str
    session_id: str
    run_id: int
    fence: int


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def acquire_lease(account_id: str, session_id: str, run_id: int,
                        ttl_s: float = DEFAULT_TTL_S) -> Lease | None:
    """Take the lease if the session is free or its lease expired -> the Lease (with its new
    fence token), else None. One statement, atomic under concurrency (the loser's WHERE sees
    the winner's row)."""
    now = _now()
    async with get_session() as s:
        row = (await s.execute(text(
            "INSERT INTO agent_session_leases "
            "(account_id, session_id, run_id, lease_until, heartbeat_at, fence) "
            "VALUES (:a, :s, :r, :until, :now, 1) "
            "ON CONFLICT (account_id, session_id) DO UPDATE SET "
            "run_id = :r, lease_until = :until, heartbeat_at = :now, "
            "fence = agent_session_leases.fence + 1 "
            "WHERE agent_session_leases.lease_until < :now "
            "RETURNING run_id, fence"),
            {"a": account_id, "s": session_id, "r": run_id,
             "until": now + timedelta(seconds=ttl_s), "now": now})).first()
    if row is None or row[0] != run_id:
        return None
    return Lease(account_id, session_id, run_id, int(row[1]))


async def renew_lease(lease: Lease, ttl_s: float = DEFAULT_TTL_S) -> bool:
    """Extend the lease of the CURRENT holder -> False when it is no longer ours."""
    now = _now()
    async with get_session() as s:
        res = await s.execute(text(
            "UPDATE agent_session_leases SET lease_until = :until, heartbeat_at = :now "
            "WHERE account_id = :a AND session_id = :s AND run_id = :r AND fence = :f"),
            {"a": lease.account_id, "s": lease.session_id, "r": lease.run_id, "f": lease.fence,
             "until": now + timedelta(seconds=ttl_s), "now": now})
    return bool(res.rowcount)


async def release_lease(lease: Lease) -> None:
    """Free the session (only if this exact lease is still current): expire it in place."""
    now = _now()
    async with get_session() as s:
        await s.execute(text(
            "UPDATE agent_session_leases SET lease_until = :past "
            "WHERE account_id = :a AND session_id = :s AND run_id = :r AND fence = :f"),
            {"a": lease.account_id, "s": lease.session_id, "r": lease.run_id, "f": lease.fence,
             "past": now - _RELEASED})


async def check_fence(session: AsyncSession, lease: Lease | None) -> None:
    """Inside the caller's write transaction: lock the lease row and require that `lease` is
    still the current holder (same run AND same fence token), else StaleLease. The row lock
    makes a concurrent takeover wait until this transaction commits. No lease (a run that holds
    none) = nothing to check."""
    if lease is None:
        return
    row = (await session.execute(
        select(AgentSessionLeaseRow.run_id, AgentSessionLeaseRow.fence).where(
            AgentSessionLeaseRow.account_id == lease.account_id,
            AgentSessionLeaseRow.session_id == lease.session_id).with_for_update()
    )).first()
    if row is None or row[0] != lease.run_id or row[1] != lease.fence:
        raise StaleLease(
            f"run {lease.run_id} lost session {lease.account_id}/{lease.session_id} "
            f"(fence {lease.fence})")


async def _heartbeat(lease: Lease, ttl_s: float, every_s: float) -> None:
    while True:
        await asyncio.sleep(every_s)
        try:
            if not await renew_lease(lease, ttl_s):
                log.error("agent session lease %s/%s lost by run %d (fence %d)",
                          lease.account_id, lease.session_id, lease.run_id, lease.fence)
                return
        except Exception:  # noqa: BLE001 - a DB blip: retry next beat, the TTL absorbs it
            log.warning("agent session lease heartbeat failed for run %d", lease.run_id,
                        exc_info=True)


@contextlib.asynccontextmanager
async def session_lock(account_id: str, session_id: str, run_id: int = 0, *,
                       ttl_s: float = DEFAULT_TTL_S,
                       heartbeat_s: float = DEFAULT_HEARTBEAT_S) -> AsyncIterator[Lease | None]:
    """Yields the held Lease (heartbeat running, expired on exit), or None when someone else
    holds a live lease."""
    lease = await acquire_lease(account_id, session_id, run_id, ttl_s)
    if lease is None:
        yield None
        return
    beat = asyncio.create_task(_heartbeat(lease, ttl_s, min(heartbeat_s, ttl_s / 3)))
    try:
        yield lease
    finally:
        beat.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await beat
        with contextlib.suppress(Exception):   # an unreleased row just expires after the TTL
            await release_lease(lease)
