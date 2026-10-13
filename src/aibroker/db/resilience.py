"""Retry harness for terminal DB writes that must survive a Postgres blip.

A successful provider call is already BILLED by the time we record it — if the
usage/result write dies on a transient connection error, the money is spent but
untracked (cost caps drift) and, for jobs, the client never sees a result the
broker paid for. Wrap ONLY terminal writes in this: reads and mid-flow writes
may fail fast and let the normal failover handle it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any, TypeVar

from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

log = logging.getLogger(__name__)

T = TypeVar("T")

# Connection-level failures worth retrying (server restart, dropped socket,
# pool poisoned). Anything else — IntegrityError, programming errors — is a
# real bug and must surface immediately, not be retried into place.
_TRANSIENT = (OperationalError, InterfaceError, ConnectionError, OSError)

_ATTEMPTS = 3
_BASE_DELAY_S = 0.25  # 0.25 → 0.5 → (fail); a restart's first seconds


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT):
        return True
    # SQLAlchemy wraps driver errors in DBAPIError with the flag set for
    # connection-invalidating failures (asyncpg ConnectionDoesNotExistError…).
    return isinstance(exc, DBAPIError) and exc.connection_invalidated


def retry_terminal_write(
    fn: Callable[..., Awaitable[T]],
) -> Callable[..., Awaitable[T]]:
    """Retry a terminal-write coroutine on transient connection failures.

    Backoff sleep here is a bounded recovery wait for a known-down dependency,
    not a polling loop. After the last attempt the original exception is
    re-raised — callers still see a hard failure when Postgres is truly gone."""

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        last: BaseException | None = None
        for attempt in range(_ATTEMPTS):
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                if not _is_transient(exc):
                    raise
                last = exc
                if attempt < _ATTEMPTS - 1:
                    delay = _BASE_DELAY_S * (2**attempt)
                    log.warning(
                        "%s: transient DB failure (attempt %d/%d), retrying in %.2fs: %s",
                        fn.__name__, attempt + 1, _ATTEMPTS, delay, exc,
                    )
                    await asyncio.sleep(delay)
        assert last is not None
        log.error("%s: terminal write failed after %d attempts", fn.__name__, _ATTEMPTS)
        raise last

    return wrapper


def is_stale_statement_error(exc: BaseException) -> bool:
    """True only for asyncpg's InvalidCachedStatementError ("cached statement plan
    is invalid due to a database schema or configuration change") as surfaced
    through SQLAlchemy: a DBAPIError whose `orig` is the dialect-adapted
    InvalidCachedStatementError (or whose `__cause__` is asyncpg's own). Matched by
    class name along the MRO so the check does not depend on the adapter's private
    class path. Any other DBAPIError is NOT matched."""
    if not isinstance(exc, DBAPIError):
        return False
    orig = exc.orig
    for candidate in (orig, getattr(orig, "__cause__", None)):
        if candidate is not None and any(
            c.__name__ == "InvalidCachedStatementError" for c in type(candidate).__mro__
        ):
            return True
    return False


def retry_stale_statement(
    fn: Callable[..., Awaitable[T]],
) -> Callable[..., Awaitable[T]]:
    """Retry ONCE when a statement hits InvalidCachedStatementError.

    After DDL on a hot table (e.g. ALTER TABLE ... ADD COLUMN) a pooled server
    backend can still hold a prepared statement whose result type changed; the
    first execution fails, and a fresh attempt (new transaction, statement
    re-prepared or the backend's cache invalidated by the driver) normally
    succeeds. The wrapped coroutine MUST open its own session/transaction so the
    retry runs in a fresh one. A second consecutive failure propagates."""

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return await fn(*args, **kwargs)
        except DBAPIError as exc:
            if not is_stale_statement_error(exc):
                raise
            log.warning(
                "%s: stale prepared statement after schema change, retrying once: %s",
                fn.__name__, exc,
            )
        return await fn(*args, **kwargs)

    return wrapper
