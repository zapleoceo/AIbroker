"""Exclusive per-(account_id, session_id) lease with fencing: two runs never drive one CLI session.

A lease ROW, not a database session lock, so the worker needs nothing but the pgbouncer
(transaction pooling) connection: no direct Postgres connection, no advisory lock, no LISTEN,
no session-level state of any kind.

- acquire: one atomic `INSERT ... ON CONFLICT DO UPDATE ... WHERE lease_until < now` - it
  succeeds only if the session is free or its lease has EXPIRED, and every successful
  acquisition bumps the row's FENCE token (monotonic per session). "now" is the DATABASE clock
  on Postgres (workers with skewed clocks agree); the app clock is used only by the SQLite
  branch of the unit tests;
- heartbeat: while the run is alive a background task extends `lease_until` every
  `heartbeat_s`; the TTL is several heartbeats long, so a crashed worker's lease expires. If a
  renewal is refused, or none succeeds before the local (monotonic) deadline, `Lease.lost` is
  set and the executor stops the CLI at once;
- fencing: a run's writes (events, status, result, usage, workspace) call `check_fence` in
  their own transaction, which locks the lease row and rejects (`StaleLease`) a holder whose
  lease was taken over - a zombie worker can never write after its successor started;
- release: the row is expired in place (never deleted), so the fence counter survives and a
  later holder always gets a larger token.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db import get_session
from aibroker.db.models import AgentSessionLeaseRow

log = logging.getLogger(__name__)

DEFAULT_TTL_S = 120.0
DEFAULT_HEARTBEAT_S = 30.0
_RELEASED_S = 86400.0               # a released row's lease_until: a day in the past


class StaleLease(Exception):
    """This run no longer holds its session lease (taken over): it must not write."""


@dataclass(frozen=True)
class Lease:
    """A held lease and its fence token. `lost` is set by the heartbeat when the lease can no
    longer be assumed ours (renewal refused, or no renewal before the local deadline)."""
    account_id: str
    session_id: str
    run_id: int
    fence: int
    lost: asyncio.Event = field(default_factory=asyncio.Event, compare=False, repr=False)


def _now() -> datetime:
    """The APP clock - used ONLY by the SQLite branch (unit tests). On Postgres every
    comparison and every stored timestamp comes from the database clock."""
    return datetime.now(UTC).replace(tzinfo=None)


_PG_NOW = "(now() AT TIME ZONE 'utc')"


def _clock(session: AsyncSession, ttl_s: float) -> tuple[str, str, str, dict[str, object]]:
    """-> (now SQL, lease_until SQL, released lease_until SQL, params). Postgres: the database's
    own clock. SQLite (tests): the app clock."""
    if session.bind.dialect.name == "postgresql":
        return (_PG_NOW, f"{_PG_NOW} + make_interval(secs => :ttl)",
                f"{_PG_NOW} - make_interval(secs => :past)",
                {"ttl": float(ttl_s), "past": _RELEASED_S})
    now = _now()
    return (":now", ":until", ":past",
            {"now": now, "until": now + timedelta(seconds=ttl_s),
             "past": now - timedelta(seconds=_RELEASED_S)})


async def acquire_lease(account_id: str, session_id: str, run_id: int,
                        ttl_s: float = DEFAULT_TTL_S) -> Lease | None:
    """Take the lease if the session is free or its lease expired -> the Lease (with its new
    fence token), else None. One statement, atomic under concurrency (the loser's WHERE sees
    the winner's row); "expired" is judged by the database clock."""
    async with get_session() as s:
        now, until, _past, params = _clock(s, ttl_s)
        row = (await s.execute(text(
            "INSERT INTO agent_session_leases "
            "(account_id, session_id, run_id, lease_until, heartbeat_at, fence) "
            f"VALUES (:a, :s, :r, {until}, {now}, 1) "
            "ON CONFLICT (account_id, session_id) DO UPDATE SET "
            f"run_id = :r, lease_until = {until}, heartbeat_at = {now}, "
            "fence = agent_session_leases.fence + 1 "
            f"WHERE agent_session_leases.lease_until < {now} "
            "RETURNING run_id, fence"),
            {"a": account_id, "s": session_id, "r": run_id, **params})).first()
    if row is None or row[0] != run_id:
        return None
    return Lease(account_id, session_id, run_id, int(row[1]))


async def renew_lease(lease: Lease, ttl_s: float = DEFAULT_TTL_S) -> bool:
    """Extend the lease of the CURRENT holder -> False when it is no longer ours."""
    async with get_session() as s:
        now, until, _past, params = _clock(s, ttl_s)
        res = await s.execute(text(
            f"UPDATE agent_session_leases SET lease_until = {until}, heartbeat_at = {now} "
            "WHERE account_id = :a AND session_id = :s AND run_id = :r AND fence = :f"),
            {"a": lease.account_id, "s": lease.session_id, "r": lease.run_id, "f": lease.fence,
             **params})
    return bool(res.rowcount)


async def release_lease(lease: Lease) -> None:
    """Free the session (only if this exact lease is still current): expire it in place."""
    async with get_session() as s:
        _now_sql, _until, past, params = _clock(s, 0)
        await s.execute(text(
            f"UPDATE agent_session_leases SET lease_until = {past} "
            "WHERE account_id = :a AND session_id = :s AND run_id = :r AND fence = :f"),
            {"a": lease.account_id, "s": lease.session_id, "r": lease.run_id, "f": lease.fence,
             **params})


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
    """Keep the lease alive; set `lease.lost` when it must no longer be assumed ours: the
    renewal was refused (taken over), or no renewal succeeded before the local deadline (the
    database is unreachable for longer than the TTL). The deadline is on the local MONOTONIC
    clock, counted from the START of the last successful renewal request, so it is never later
    than the database's lease_until."""
    deadline = time.monotonic() + ttl_s
    while True:
        await asyncio.sleep(every_s)
        started = time.monotonic()
        try:
            ok = await asyncio.wait_for(renew_lease(lease, ttl_s),
                                        timeout=max(deadline - started, 0.001))
        except Exception:  # noqa: BLE001 - DB blip / hang: the local deadline decides
            log.warning("agent session lease heartbeat failed for run %d", lease.run_id,
                        exc_info=True)
        else:
            if not ok:
                log.error("agent session lease %s/%s lost by run %d (fence %d)",
                          lease.account_id, lease.session_id, lease.run_id, lease.fence)
                lease.lost.set()
                return
            deadline = started + ttl_s
        if time.monotonic() >= deadline:
            log.error("agent session lease %s/%s of run %d expired locally (no renewal in %.0fs)",
                      lease.account_id, lease.session_id, lease.run_id, ttl_s)
            lease.lost.set()
            return


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
