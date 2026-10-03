"""Cache affinity — keep a request family on the (provider, model, key) whose
provider-side prompt cache is already warm.

Two layers, ONE module, ONE TTL (`AFFINITY_TTL_S`, default 2h):

  * KEY pin, (project, provider) -> api_key_id. The original mechanism: the
    selector prefers the pinned key as a tie-break (and cache-sticky providers pin
    to it outright). Picks a KEY inside a provider.
  * ROUTE pin, (project, workflow, capability, pinned model) -> (provider, model,
    key_id). Added for ALL capabilities: picks the whole target. A new request
    tries it first and moves on only when it is cooling / capped / errored; the
    walk then re-pins to whatever succeeded.

Both live in `shared_state` (Redis, shared across workers, fail-open) with an
in-process dict as the fallback, so a single node / Redis-down / SQLite test run
behaves identically. The same affinity key also seeds the stable
`prompt_cache_key` sent to providers that document one (see prompt_cache).
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from aibroker.config import get_settings
from aibroker.providers.registry import paid_providers
from aibroker.routing import shared_state


def ttl_s() -> float:
    """Affinity TTL in seconds (settings.AFFINITY_TTL_S)."""
    return get_settings().AFFINITY_TTL_S


# ─── KEY pin: (project, provider) -> api_key_id ─────────────────────────────

_affinity: dict[tuple[int, str], tuple[int, float]] = {}


def _note_affinity(project_id: int, provider: str, api_key_id: int) -> None:
    """Pin the key that just successfully served (project, provider).
    Internal — services go through note_affinity_shared so the pin also reaches
    the cross-worker store."""
    _affinity[(project_id, provider)] = (api_key_id, time.monotonic())


async def note_affinity_shared(project_id: int, provider: str, api_key_id: int) -> None:
    """_note_affinity + publish the pin to the cross-worker store (fail-open)."""
    _note_affinity(project_id, provider, api_key_id)
    await shared_state.set_affinity(project_id, provider, api_key_id, ttl_s())


def _affinity_for(project_id: int | None, provider: str) -> int | None:
    if project_id is None:
        return None
    entry = _affinity.get((project_id, provider))
    if entry is None:
        return None
    api_key_id, noted_at = entry
    if time.monotonic() - noted_at > ttl_s():
        del _affinity[(project_id, provider)]
        return None
    return api_key_id


async def _affinity_for_shared(project_id: int | None, provider: str) -> int | None:
    """Cross-worker pin first; in-process dict as the fallback/miss path."""
    if project_id is None:
        return None
    shared = await shared_state.get_affinity(project_id, provider)
    if shared is not None:
        return shared
    return _affinity_for(project_id, provider)


# ─── ROUTE pin: (project, workflow, capability, pin) -> (provider, model, key) ──


@dataclass(frozen=True, slots=True)
class AffinityTarget:
    provider: str
    model: str
    key_id: int


def route_key(project_id: int, workflow: str | None, capability: str,
              pinned_model: str | None) -> str:
    """Redis/dict key for a request family. workflow and pin are '' when absent."""
    digest = hashlib.sha256(
        f"{workflow or ''}\x1f{capability}\x1f{pinned_model or ''}".encode()).hexdigest()[:20]
    return f"aib:raff:{project_id}:{digest}"


_routes: dict[str, tuple[AffinityTarget, float]] = {}


def reset() -> None:
    """Drop every in-process pin (tests / ops)."""
    _affinity.clear()
    _routes.clear()


async def lookup_route(project_id: int, workflow: str | None, capability: str,
                       pinned_model: str | None) -> AffinityTarget | None:
    """The pinned target for this request family, or None (miss / expired)."""
    key = route_key(project_id, workflow, capability, pinned_model)
    raw = await shared_state.get_json(key)
    if isinstance(raw, dict) and {"p", "m", "k"} <= raw.keys():
        try:
            return AffinityTarget(str(raw["p"]), str(raw["m"]), int(raw["k"]))
        except (TypeError, ValueError):
            pass
    entry = _routes.get(key)
    if entry is None:
        return None
    target, noted_at = entry
    if time.monotonic() - noted_at > ttl_s():
        del _routes[key]
        return None
    return target


async def note_route(project_id: int, workflow: str | None, capability: str,
                     pinned_model: str | None, target: AffinityTarget) -> None:
    key = route_key(project_id, workflow, capability, pinned_model)
    _routes[key] = (target, time.monotonic())
    await shared_state.set_json(
        key, {"p": target.provider, "m": target.model, "k": target.key_id}, ttl_s())


async def note_success(
    project_id: int, workflow: str | None, capability: str, pinned_model: str | None,
    provider: str, model: str, key_id: int,
) -> None:
    """Re-pin BOTH layers to what just succeeded."""
    await note_affinity_shared(project_id, provider, key_id)
    await note_route(project_id, workflow, capability, pinned_model,
                     AffinityTarget(provider, model, key_id))


def may_promote(target: AffinityTarget, head_provider: str | None, *, paid_only: bool) -> bool:
    """Whether the affine target may jump ahead of the walk's first step.

    Free-first is policy: a PAID affine provider is never promoted over a free
    head (the 2026-07-12 decision not to re-order chains per project stands for
    exactly that case). Everything else — including free -> free and
    paid -> paid — may keep its warm cache."""
    if paid_only:
        return True
    paid = paid_providers()
    return not (target.provider in paid and head_provider not in paid)
