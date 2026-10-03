"""The ONE key-attempt template.

Every capability (chat, vision, embedding, transcription, decision) runs a single
attempt the same way:

    reserve cost -> decrypt key -> call -> release reservation
        -> quality gate -> record usage -> update cache affinity

with ONE error path (model-unavailable / penalize / size learning / booking).
A runner supplies only what differs: the `call` closure, an optional `check`
gate and a few flags. The runners (services/llm_service.py) own the walk over
keys and providers; this module owns what happens to a single key.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from typing import Any

from aibroker.crypto import decrypt
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.providers.context_limits import is_too_large_error
from aibroker.providers.observations import record_too_large
from aibroker.providers.provider_errors import (
    classify_provider_error,
    http_status_of,
    is_model_unavailable,
    is_timeout,
)
from aibroker.routing import (
    CostGuardError,
    affinity,
    circuit,
    release_cost,
    reserve_cost,
    scope_for,
)
from aibroker.routing.model_cooldown import mark_model_cooldown
from aibroker.routing.selector import mark_cooldown, mark_dead, record_usage
from aibroker.telemetry import audit
from aibroker.telemetry.request_context import current_request_id

log = logging.getLogger(__name__)

_COOLDOWN = timedelta(minutes=5)


class Flow(Enum):
    """Verdict of one key attempt — how the runner's walk proceeds."""
    NEXT_KEY = auto()
    NEXT_PROVIDER = auto()
    BUDGET_EXHAUSTED = auto()  # project/global cap spent — abort the whole walk
    SUCCESS = auto()


# Key-shaped substrings a provider may echo back in an error body. The dashboard
# renders `last_error` verbatim and it lands in every DB backup, so scrub BEFORE
# persisting — docs/security.md promises provider keys are never logged.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),          # openai/anthropic/deepseek-style
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),         # google
    re.compile(r"\bgsk_[A-Za-z0-9]{16,}"),             # groq
    re.compile(r"\bcsk-[A-Za-z0-9]{16,}"),             # cerebras
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|key)=[^&\s\"']{8,}"),
)


def _scrub_secrets(text: str) -> str:
    """Replace anything that looks like a credential with a fixed marker."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[redacted]", text)
    return text


def _billed_cost(key: ApiKeyRow, meta: dict[str, Any]) -> float:
    """What we actually owe the provider for this call.

    Pricing is by MODEL and knows nothing about plans, so a free-tier key calling
    a priced model would book the nominal price although the free plan absorbs it.
    Free-tier keys always bill $0; the `tier` column is the source of truth. (If a
    free account ever exhausts its free allocation, flip that key to tier='paid'.)
    """
    return 0.0 if key.tier == "free" else meta["cost_usd"]


async def _release_reservation(key: ApiKeyRow, estimated_cost: float) -> None:
    """Refund a reserve_cost reservation. Shielded from cancellation (a client
    disconnect cancelling the request mid-refund must still finish it) and never
    raises: a failed refund only leaves the daily counter conservatively high —
    it must not mask the real outcome of the attempt it is cleaning up after.

    Every path that called reserve_cost reaches this exactly once, INCLUDING
    asyncio.CancelledError (a BaseException that `except Exception` misses) and a
    failure between the reserve and the provider call such as decrypt()
    (2026-10-03 review: both leaked the reservation until midnight)."""
    try:
        await asyncio.shield(release_cost(api_key=key, estimated_cost=estimated_cost))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        log.exception("release_cost failed for key %s — reservation of $%.4f leaks "
                      "until the daily reset", key.id, estimated_cost)


async def _penalize(key: ApiKeyRow, exc: Exception, *, capability: str | None = None,
                    model: str | None = None) -> str:
    """Cooldown on rate-limit, mark dead on auth error. Returns the error kind.
    `capability` scopes the timeout circuit-breaker (see routing/circuit).
    `model` is the model the failed call used: a quota metered per (key, model)
    (gemini's per-day free tier) parks only that pair, not the whole key — see
    routing/model_cooldown."""
    kind = classify_provider_error(exc, key.provider)
    # Short human-readable reason surfaced on the dashboard (what killed it / when
    # a cooldown ends) — scrubbed of anything key-shaped first.
    reason = _scrub_secrets(str(exc))[:200]
    timed_out = is_timeout(exc)
    if timed_out:
        # Feed the selection-side circuit-breaker so a bulk-timing-out provider
        # is soft-skipped and this hung key isn't re-pinned by affinity.
        circuit.note_timeout(key.provider, key.id,
                             scope=scope_for(capability) if capability else None)
    if kind == "rate_limit":
        # Cooldown resolved by the provider's own signal — retry-after hint >
        # daily quota (until UTC midnight) > adaptive backoff. One session for
        # the whole penalty (adaptive COUNT + cooldown UPDATE).
        from aibroker.routing.cooldown import cooldown_until, is_model_scoped_quota
        model_scoped = bool(model) and is_model_scoped_quota(key.provider, str(exc))
        async with get_session() as s:
            try:
                until = await cooldown_until(key.id, key.provider, str(exc),
                                             session=s, is_timeout=timed_out)
            except Exception:
                # A failed statement aborts the tx on Postgres — roll back so
                # the fallback UPDATE below can still land in this session.
                await s.rollback()
                until = datetime.now(UTC) + _COOLDOWN
            if model_scoped:
                await mark_model_cooldown(key.id, model, until, reason, session=s)
            else:
                await mark_cooldown(key.id, until, reason, session=s)
    elif kind == "auth":
        await mark_dead(key.id, reason)
        # Traffic-side deaths must be traceable: the monitor alerts on ITS
        # deaths; this path books its own.
        await audit(actor="system:llm_service", action="key.dead",
                    target=f"key:{key.id}",
                    metadata={"provider": key.provider, "label": key.label,
                              "capability": capability, "reason": reason})
    return kind


@dataclass(frozen=True)
class Rejection:
    """A billed-but-unusable answer found by a quality gate (`Attempt.check`)."""
    error_kind: str
    flow: Flow
    http_status: int = 200
    bill: bool = True                       # carry the call's tokens/cost into the row
    note: Callable[[], None] | None = None  # side effect, e.g. circuit.note_empty_body
    log_message: str = ""


@dataclass
class Attempt:
    """Everything one attempt needs. `call(plain_key)` performs the provider call
    and returns `(payload, meta)`; `check(payload, meta)` may veto the result."""
    key: ApiKeyRow
    project: ProjectRow
    provider: str
    model: str
    capability: str
    workflow: str | None
    call: Callable[[str], Awaitable[tuple[Any, dict[str, Any]]]]
    estimated_cost: float = 0.0
    est_tokens: int = 0                     # >0 enables self-learning of size ceilings
    check: Callable[[Any, dict[str, Any]], Rejection | None] | None = None
    cap_flow: Flow = Flow.NEXT_PROVIDER     # verdict for a PER-KEY cap block
    reserve: bool = True                    # False: $0 call that must not be refused by a cap
    penalize: bool = True                   # False: book the error but never cool/kill the key
    pinned_model: str | None = None         # part of the affinity key
    note_affinity: bool = True


@dataclass
class AttemptResult:
    flow: Flow
    payload: Any = None
    meta: dict[str, Any] = field(default_factory=dict)
    usage_id: int | None = None
    error: Exception | None = None          # the exception behind a non-SUCCESS flow
    rejection: str | None = None            # error_kind of a quality-gate veto (EmptyBody...)


# classify_provider_error verdict -> usage_log.status (init.sql vocabulary).
_STATUS_BY_KIND = {"rate_limit": "rate_limit", "auth": "auth_fail"}


async def _record_error(
    *, key: ApiKeyRow, project: ProjectRow, provider: str, model: str,
    capability: str, workflow: str | None, exc: Exception,
) -> None:
    """Book a failed attempt in usage_log. A failed attempt always books $0
    (the reservation is fully released): answerless calls must not consume
    admission budget reserved for ANSWERS; real upstream timeout spend is
    reconciled against the provider invoice out-of-band (2026-07-12 / 07-16).

    `http_status` is the status the provider REALLY returned (None when the
    exception carries none: timeouts, network errors). It used to be fabricated
    from our classification (rate_limit -> 429) so adaptive_cooldown could count
    429 rows (fix 2026-07-10) — that corrupted reporting. The escalation signal
    now rides `status` (ok|rate_limit|auth_fail|error): cooldown counts
    `status = 'rate_limit'` rows (plus legacy http_status=429 rows)."""
    kind = classify_provider_error(exc, provider)
    await record_usage(
        api_key_id=key.id, project_id=project.id, lease_id=None,
        provider=provider, model=model, capability=capability,
        workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
        latency_ms=None, status=_STATUS_BY_KIND.get(kind, "error"),
        error_kind=type(exc).__name__, http_status=http_status_of(exc),
        request_id=current_request_id(),
    )


async def _handle_call_error(a: Attempt, exc: Exception) -> Flow:
    """Classify a failed call, penalize/book it, return the walk verdict."""
    book = {"key": a.key, "project": a.project, "provider": a.provider, "model": a.model,
            "capability": a.capability, "workflow": a.workflow, "exc": exc}
    # Model gone/unprovisioned (404): a MODEL problem, not a KEY one — this key's
    # other models work and sibling keys run the same dead model. Do NOT penalize
    # the key; move to the next provider.
    if is_model_unavailable(exc):
        await _record_error(**book)
        log.warning("provider %s model %s unavailable (%s) — next provider",
                    a.provider, a.model, type(exc).__name__)
        return Flow.NEXT_PROVIDER
    kind = (await _penalize(a.key, exc, capability=a.capability, model=a.model)) if a.penalize else \
        classify_provider_error(exc, a.provider)
    # Self-learn the size ceiling: a provider that rejects the prompt as too big
    # is skipped for prompts >= this size next time (no hardcoded cap).
    if a.est_tokens and is_too_large_error(exc):
        await record_too_large(a.provider, a.est_tokens)
        log.info("learned: %s rejects ~%d tok prompts", a.provider, a.est_tokens)
        await _record_error(**book)
        return Flow.NEXT_PROVIDER  # bigger keys won't help
    await _record_error(**book)
    log.warning("provider %s key %s failed (%s): %s", a.provider, a.key.label, kind, exc)
    return Flow.NEXT_KEY


async def _block_on_cap(a: Attempt, e: CostGuardError) -> Flow:
    await audit(actor=f"project:{a.project.name}", action="cap_block",
                target=f"provider={a.provider}", metadata={"reason": str(e)})
    # Book the block exactly like every other failed attempt (error, CapBlock,
    # 402, $0) so cap-blocked picks show in the usage view, not only in audit_log.
    await record_usage(
        api_key_id=a.key.id, project_id=a.project.id, lease_id=None,
        provider=a.provider, model=a.model, capability=a.capability,
        workflow=a.workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
        latency_ms=None, status="error", error_kind="CapBlock",
        http_status=402, request_id=current_request_id(),
    )
    # A project/global cap blocks EVERY paid provider identically, so walking on is
    # futile spend — abort the walk. A per-key cap is local: the runner decides.
    if e.kind in ("project", "global"):
        return Flow.BUDGET_EXHAUSTED
    return a.cap_flow


async def _record_rejection(a: Attempt, rej: Rejection, meta: dict[str, Any]) -> None:
    billed = rej.bill
    await record_usage(
        api_key_id=a.key.id, project_id=a.project.id, lease_id=None,
        provider=a.provider, model=a.model, capability=a.capability,
        workflow=a.workflow,
        tokens_in=meta.get("tokens_in", 0) if billed else 0,
        tokens_out=meta.get("tokens_out", 0) if billed else 0,
        cost_usd=meta.get("cost_usd", 0.0) if billed else 0.0,
        cache_read_tokens=meta.get("cache_read_tokens", 0) if billed else 0,
        cache_write_tokens=meta.get("cache_write_tokens", 0) if billed else 0,
        latency_ms=meta.get("latency_ms"), status="error",
        error_kind=rej.error_kind, http_status=rej.http_status,
        request_id=current_request_id(),
    )
    if rej.note:
        rej.note()
    if rej.log_message:
        log.warning(rej.log_message)


async def run_attempt(a: Attempt) -> AttemptResult:
    """Run one key attempt to a verdict (see module docstring)."""
    reserved = a.estimated_cost if (a.reserve and a.estimated_cost > 0) else 0.0
    if a.reserve:
        try:
            await reserve_cost(api_key=a.key, project=a.project,
                               estimated_cost=a.estimated_cost)
        except CostGuardError as e:
            return AttemptResult(await _block_on_cap(a, e), error=e)
    try:
        # decrypt INSIDE the try: it runs after reserve_cost, so a failure here
        # must release the reservation like any other attempt failure.
        plain = decrypt(a.key.token_encrypted)
        payload, meta = await a.call(plain)
        meta["cost_usd"] = _billed_cost(a.key, meta)
    except BaseException as e:  # noqa: BLE001 — classify, cool the key, try next
        # The attempt is over however it ends — fully release the reservation so
        # an answerless call (incl. a paid timeout) consumes NO admission budget.
        # BaseException so a cancelled request (CancelledError) releases too; it
        # is re-raised untouched.
        if reserved:
            await _release_reservation(a.key, reserved)
        if not isinstance(e, Exception):
            raise
        return AttemptResult(await _handle_call_error(a, e), error=e)
    # Resolved: release the reservation; record_usage books the REAL final cost
    # on top, so the key ends up debited by exactly the real cost.
    if reserved:
        await _release_reservation(a.key, reserved)

    if a.check is not None and (rej := a.check(payload, meta)) is not None:
        await _record_rejection(a, rej, meta)
        return AttemptResult(rej.flow, payload=payload, meta=meta, rejection=rej.error_kind)

    usage_id = await record_usage(
        api_key_id=a.key.id, project_id=a.project.id, lease_id=None,
        provider=a.provider, model=a.model, model_served=meta.get("model_served"),
        capability=a.capability, workflow=a.workflow,
        tokens_in=meta.get("tokens_in", 0), tokens_out=meta.get("tokens_out", 0),
        cost_usd=meta["cost_usd"],
        cache_read_tokens=meta.get("cache_read_tokens", 0),
        cache_write_tokens=meta.get("cache_write_tokens", 0),
        latency_ms=meta.get("latency_ms"), status="ok", error_kind=None,
        http_status=200, request_id=current_request_id(),
    )
    # A success re-pins the request family AND (project, provider) to what just
    # worked, so the NEXT request lands where the provider-side cache is warm.
    if a.note_affinity:
        await affinity.note_success(a.project.id, a.workflow, a.capability,
                                    a.pinned_model, a.provider, a.model, a.key.id)
    return AttemptResult(Flow.SUCCESS, payload=payload, meta=meta, usage_id=usage_id)
