"""Long-poll delivery of job results: GET /v1/jobs/{id}?wait=N and POST /v1/jobs/wait.

A waiter holds no DB session and no thread while it waits: it registers an
asyncio.Event for its job ids in this process and sleeps on it. Two things set it:

- Redis pub/sub (fast path, sub-second): every terminal transition written through
  `deep_jobs._finish` (chat, structured, vision, transcription, image, video, vivix)
  publishes the job id on CHANNEL; one subscriber task per process
  wakes the matching waiters.
- A per-process sweep (safety net): while anyone waits, ONE query per SWEEP_S checks
  every registered id for a terminal status, so a transition written by a path that
  does not publish (agent runs, stale-run reaper, media reconciliation) or a Redis outage still
  wakes the waiter within about a second. DB load is bounded per process, not per waiter.

Lost-wakeup race: a waiter registers first, then reads the DB once; a transition that
lands between the caller's read and the registration is caught by that read.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import Counter
from typing import Any

from sqlalchemy import select

from aibroker.db.engine import get_session
from aibroker.db.models import DeepJobRow

log = logging.getLogger(__name__)

CHANNEL = "aib:jobs:terminal"
TERMINAL_STATUSES = ("done", "error", "cancelled")
MAX_WAIT_S = 25          # well below nginx (120 s proxy_read_timeout) and Cloudflare (100 s)
MAX_IDS = 100
MAX_WAITERS_PER_PROJECT = 200
RETRY_AFTER_S = 2
SWEEP_S = 1.0

_waiters: dict[int, set[asyncio.Event]] = {}
_per_project: Counter[int] = Counter()
_tasks: list[asyncio.Task[None]] = []
_tasks_loop: asyncio.AbstractEventLoop | None = None
_pub_client: Any = None


class TooManyWaiters(Exception):
    """The project already has MAX_WAITERS_PER_PROJECT open long polls (HTTP 429)."""


def _redis_url() -> str:
    return os.environ.get("REDIS_URL", "")


def _wake(job_id: int) -> None:
    for event in _waiters.get(job_id, ()):
        event.set()


async def publish_terminal(job_id: int) -> None:
    """Announce that `job_id` reached a terminal status. Best effort: a missing or sick
    Redis only costs the waiter the sweep latency (about SWEEP_S), never an error."""
    _wake(job_id)                          # waiters in this very process, no round trip
    global _pub_client
    url = _redis_url()
    if not url:
        return
    try:
        if _pub_client is None:
            import redis.asyncio as redis_asyncio
            _pub_client = redis_asyncio.from_url(url, socket_connect_timeout=0.5,
                                                 socket_timeout=0.5)
        await _pub_client.publish(CHANNEL, str(job_id))
    except Exception as e:  # noqa: BLE001 — a wake-up hint must never fail a job write
        log.debug("job_wait: publish %s failed: %s", job_id, e)


async def _subscribe_forever() -> None:  # pragma: no cover — needs a live Redis
    url = _redis_url()
    if not url:
        return
    import redis.asyncio as redis_asyncio
    while True:
        try:
            client = redis_asyncio.from_url(url, socket_connect_timeout=2,
                                            socket_timeout=None, health_check_interval=30)
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(CHANNEL)
            async for message in pubsub.listen():
                if message.get("type") == "message":
                    try:
                        _wake(int(message["data"]))
                    except (TypeError, ValueError):
                        continue
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the sweep covers the outage
            log.warning("job_wait: redis subscriber down (%s); sweep-only for now", e)
            await asyncio.sleep(5)


async def terminal_ids(ids: list[int]) -> set[int]:
    if not ids:
        return set()
    async with get_session() as s:
        rows = await s.scalars(select(DeepJobRow.id).where(
            DeepJobRow.id.in_(ids), DeepJobRow.status.in_(TERMINAL_STATUSES)))
        return set(rows.all())


async def _sweep_forever() -> None:
    while True:
        await asyncio.sleep(SWEEP_S)
        if not _waiters:
            continue
        try:
            for job_id in await terminal_ids(list(_waiters)):
                _wake(job_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the next sweep retries
            log.warning("job_wait: sweep failed: %s", e)


def _ensure_started() -> None:
    global _tasks_loop
    loop = asyncio.get_running_loop()
    if _tasks and _tasks_loop is loop and not any(t.done() for t in _tasks):
        return
    for t in _tasks:
        if not t.done() and _tasks_loop is loop:
            t.cancel()
    _tasks_loop = loop
    _tasks[:] = [asyncio.create_task(_sweep_forever(), name="job-wait-sweep"),
                 asyncio.create_task(_subscribe_forever(), name="job-wait-redis")]


async def stop() -> None:
    """Shutdown hook: cancel the per-process sweep and subscriber."""
    for t in _tasks:
        t.cancel()
    _tasks.clear()


async def wait_any(project_id: int, ids: list[int], wait_s: float) -> None:
    """Return as soon as any of `ids` is terminal, or after `wait_s` seconds.
    Raises TooManyWaiters when the project already holds MAX_WAITERS_PER_PROJECT."""
    if not ids or wait_s <= 0:
        return
    if _per_project[project_id] >= MAX_WAITERS_PER_PROJECT:
        raise TooManyWaiters
    _ensure_started()
    event = asyncio.Event()
    _per_project[project_id] += 1
    for job_id in ids:
        _waiters.setdefault(job_id, set()).add(event)
    try:
        if await terminal_ids(ids):        # registered first: no lost wake-up
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), timeout=min(wait_s, MAX_WAIT_S))
    finally:
        for job_id in ids:
            group = _waiters.get(job_id)
            if group is not None:
                group.discard(event)
                if not group:
                    del _waiters[job_id]
        _per_project[project_id] -= 1
        if _per_project[project_id] <= 0:
            del _per_project[project_id]
