"""Redis-backed cross-worker store for selector hot state (fail-open).

The selector's cache-affinity map and saturation verdict used to live in
per-worker dicts — two uvicorn workers each learned them separately, and a
second node would split-brain them entirely. This module shares both via
Redis. Fail-open is the contract: no REDIS_URL, a missing redis package, or
a sick Redis must never break request serving — callers get None / no-op and
fall back to the selector's in-process dicts.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

# The affinity TTL is a setting (AFFINITY_TTL_S, default 2h) read through
# routing/affinity.ttl_s() so the Redis and in-process entries expire in step.

# After a Redis error the store stays off for this window instead of paying a
# connect timeout on EVERY pick — the selector hot path must not stall on a
# sick Redis. The next call after the window retries.
_REDIS_RETRY_S = 60.0

_SAT_KEY = "aib:sat"

# Typed Any, not redis.Redis — the import is lazy (see _get_client) so local
# venvs / SQLite CI without the package still import this module fine.
_client: Any = None
_disabled_until: float = 0.0
_warned = False  # warn once per process, like deep_jobs._dedup_available


def _aff_key(project_id: int, provider: str) -> str:
    return f"aib:aff:{project_id}:{provider}"


def _trip(exc: Exception) -> None:
    """Disable the store for _REDIS_RETRY_S after any Redis failure."""
    global _disabled_until, _warned
    _disabled_until = time.monotonic() + _REDIS_RETRY_S
    if not _warned:
        _warned = True
        log.warning(
            "shared_state: redis unavailable (%s: %s) — in-process fallback, "
            "retry in %.0fs windows", type(exc).__name__, exc, _REDIS_RETRY_S,
        )


def _get_client() -> Any:
    """Lazy singleton async Redis client; None = store disabled right now.

    REDIS_URL is read per call (not via lru_cached Settings) so tests toggle
    it with a plain monkeypatch.setenv and no cache surgery.
    """
    global _client
    if time.monotonic() < _disabled_until:
        return None
    url = os.environ.get("REDIS_URL", "")
    if not url:
        return None
    if _client is None:
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as e:
            _trip(e)
            return None
        # Sub-second timeouts: a pick must degrade, never queue behind Redis.
        _client = redis_asyncio.from_url(
            url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )
    return _client


async def get_json(key: str) -> Any | None:
    """Generic fail-open JSON read; None = miss, corrupt payload or disabled."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
    except Exception as e:
        _trip(e)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as e:
        log.warning("shared_state: corrupt %s payload (%s) — treating as miss", key, e)
        return None


async def set_json(key: str, value: Any, ttl_s: float) -> None:
    """Generic fail-open JSON write with a TTL."""
    client = _get_client()
    if client is None:
        return
    try:
        await client.setex(key, max(1, int(ttl_s)), json.dumps(value))
    except Exception as e:
        _trip(e)


async def get_affinity(project_id: int, provider: str) -> int | None:
    """Shared (project, provider) → api_key_id pin; None = miss or disabled."""
    value = await get_json(_aff_key(project_id, provider))
    return int(value) if value is not None else None


async def set_affinity(project_id: int, provider: str, key_id: int, ttl_s: float) -> None:
    await set_json(_aff_key(project_id, provider), key_id, ttl_s)


async def get_saturated() -> frozenset[int] | None:
    """Shared saturated-key-id verdict; None = cache miss (caller recomputes)."""
    client = _get_client()
    if client is None:
        return None
    try:
        raw = await client.get(_SAT_KEY)
    except Exception as e:
        _trip(e)
        return None
    if raw is None:
        return None
    try:
        return frozenset(int(x) for x in json.loads(raw))
    except (ValueError, TypeError) as e:
        # Corrupt payload = miss, not an outage: the caller recomputes from
        # the DB and overwrites it. Don't trip the whole store over one key.
        log.warning("shared_state: corrupt %s payload (%s) — treating as miss", _SAT_KEY, e)
        return None


async def set_saturated(ids: frozenset[int], ttl: float) -> None:
    client = _get_client()
    if client is None:
        return
    try:
        await client.setex(_SAT_KEY, max(1, int(ttl)), json.dumps(sorted(ids)))
    except Exception as e:
        _trip(e)
