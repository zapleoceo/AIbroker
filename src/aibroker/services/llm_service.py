"""Chat/embed orchestration.

Routes stay thin (validate → call → shape response). Everything about picking a
key, checking caps, calling the provider, classifying the error, recording usage
and walking to the next provider in the chain lives here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, auto
from typing import Any

from aibroker.crypto import decrypt
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.providers import call_llm
from aibroker.providers.context_limits import (
    MIN_LEARNABLE_CEILING,
    estimate_prompt_tokens,
    fits_context,
    is_too_large_error,
)
from aibroker.providers.litellm_adapter import (
    embed,
    estimate_llm_cost,
    extra_for_provider,
    model_for,
    transcribe,
)
from aibroker.providers.observations import learned_ceilings, record_too_large

# Re-exported: tests and services/__init__ import classify_provider_error from here.
from aibroker.providers.provider_errors import (
    classify_provider_error,
    is_model_unavailable,
)
from aibroker.routing import (
    CostGuardError,
    chain_for,
    deprioritize_for_json,
    pick_and_reserve,
    release_cost,
    reserve_cost,
    scope_for,
)
from aibroker.routing.selector import (
    mark_cooldown,
    mark_dead,
    note_affinity_shared,
    record_usage,
)
from aibroker.services import response_cache
from aibroker.telemetry import audit

log = logging.getLogger(__name__)

_COOLDOWN = timedelta(minutes=5)
# Keys tried per provider before falling through to the next provider in the
# chain (like a direct client looping a provider's keys). Free keys rate-limit
# constantly, so a few retries pay off — but gemini (tiny per-project daily cap,
# ~20/day/model on free tier) and cerebras (rolling RPM) rate-limit their keys
# in lockstep: when the first two 429, a third rarely helps, and 5 tries is just
# added latency before the chain moves on. Cap those two lower; everyone else
# keeps the full breadth.
_MAX_KEYS_PER_PROVIDER = 5
_MAX_KEYS_BY_PROVIDER: dict[str, int] = {"gemini": 3, "cerebras": 3}
# Empty/whitespace JSON bodies: retry the same provider at most this many times
# (transient throttle recovers on retry) before treating it as a real miss and
# moving on — a deterministic empty (e.g. DeepSeek json_object on a 30k prompt)
# must not burn every key of the provider.
_MAX_EMPTY_RETRIES = 1

# Absolute runaway backstop on provider-call attempts for a single request.
# The real budget is dynamic — sum of per-provider key allowances across the
# actual chain (see `_attempt_budget`), so every provider (incl. the paid tail)
# is reachable before we 503. This flat ceiling only guards against a
# pathological chain; it must stay ABOVE the longest real chain's key sum so it
# never starves the tail. Was a flat 12 — but chat:fast grew to 14 providers,
# so 12 could be consumed by early free providers and the paid tail
# (deepseek/anthropic/openai) was never reached: long dialogs 503'd during the
# 2026-07-07 incident precisely because of this. 2026-07-10: 60 → 100 — the
# chat:fast key sum had reached 61 (13 providers, cerebras/gemini 3 each + the
# rest 5), so 60 clipped the last attempt; 100 restores real headroom.
_MAX_ATTEMPTS_ABS = 100

# Per-provider-call timeout (seconds). A safety net against a hung upstream —
# normal calls finish in ~1-8s; this only cuts a genuine hang so the chain can
# fail over instead of blocking until the client's read timeout. chat:deep is
# the exception: nemotron legitimately runs minutes (it's an async job;
# job_queue._requeue_stale_running reclaims rows stuck `running` past its
# 25-min stale window), so it gets a long ceiling that still fires before the
# job is treated as stale.
#
# 2026-07-07: raised 45s -> 60s (explicit ask, applies to every key/provider).
# Trade-off worth knowing: Stepan2's own client read timeout for chat:fast is
# also 60s (llm_read_timeout_s) — a single hung attempt at this ceiling can
# now consume that entire budget, leaving no time for the chain to fail over
# to the next provider before the CLIENT gives up (a 504/abort instead of a
# clean 503). chat:smart's 90s client budget still has headroom for one hang
# + a fallback attempt. Not tightened here since the ask was explicit; flagging
# so a future chat:fast timeout tightening is an informed choice, not a
# surprise discovery.
_CALL_TIMEOUT_S = 60.0
_DEEP_CALL_TIMEOUT_S = 19 * 60.0


def _max_keys(provider: str) -> int:
    return _MAX_KEYS_BY_PROVIDER.get(provider, _MAX_KEYS_PER_PROVIDER)


def _attempt_budget(chain: list[str]) -> int:
    """Total provider-call attempts allowed for a request over `chain`: the sum
    of every provider's key allowance ("try every key we have before giving
    up"), bounded by the absolute runaway backstop. Guarantees each provider —
    including the paid tail — is reached before a 503, since a saturated
    provider returns no key and costs 0 attempts."""
    return min(_MAX_ATTEMPTS_ABS, sum(_max_keys(p) for p in chain))


def _call_timeout(capability: str) -> float:
    return _DEEP_CALL_TIMEOUT_S if capability == "chat:deep" else _CALL_TIMEOUT_S


async def _penalize(key: ApiKeyRow, exc: Exception) -> str:
    """Cooldown on rate-limit, mark dead on auth error. Returns the error kind."""
    kind = classify_provider_error(exc, key.provider)
    # Short, human-readable reason surfaced on the dashboard (2026-07-05) — the
    # dashboard used to show only "мёртв"/"пауза" with no way to tell "no
    # money" from "rate limited" apart, or when a cooldown actually ends.
    reason = str(exc)[:200]
    if kind == "rate_limit":
        # 2026-06-29: cooldown resolved by the provider's own signal —
        # retry-after hint > daily-quota (until UTC midnight) > adaptive
        # backoff. Stops the retry storm where a daily-exhausted key
        # (cerebras "tokens per day limit exceeded") got a 60 s cooldown,
        # recovered, got hammered, re-failed — looping until midnight.
        # 2026-07-16: one session for the whole penalty — the adaptive COUNT
        # and the cooldown UPDATE used to each open their own session, pure
        # pool churn on a path that fires on every failed attempt.
        from aibroker.routing.cooldown import cooldown_until
        async with get_session() as s:
            try:
                until = await cooldown_until(key.id, key.provider, str(exc),
                                             session=s)
            except Exception:
                # A failed statement aborts the tx on Postgres — roll back so
                # the fallback UPDATE below can still land in this session.
                await s.rollback()
                until = datetime.now(UTC) + _COOLDOWN
            await mark_cooldown(key.id, until, reason, session=s)
    elif kind == "auth":
        await mark_dead(key.id, reason)
    return kind


async def _record_error(
    *, key: ApiKeyRow, project: ProjectRow, provider: str, model: str,
    capability: str, workflow: str | None, exc: Exception,
) -> None:
    """Book a failed attempt in usage_log. Shared by run_chat/run_embed/
    run_transcribe — the shape is identical; only the capability differs.

    A failed attempt always books cost_usd=0. Two incidents pull opposite ways
    and this is the reconciliation:
      - 2026-07-12 ($122 gap): a paid gemini TIMEOUT was billed upstream while
        we recorded $0 — real spend UNDERcounted. That fix charged the reserved
        estimate on a timeout so the per-key cost cap could see it.
      - 2026-07-16 (storm, $0.50/day cap): with a tiny cap, a handful of
        ANSWERLESS timeouts booked at the estimate exhausted the whole day's
        ADMISSION budget on ZERO answers — starving the answers the owner
        actually reserves that budget for.
    For ADMISSION-cap purposes an answerless call must not consume budget
    reserved for ANSWERS: the reservation is fully released (release_cost, in
    _run_attempt) and the row is booked at $0. Real upstream timeout spend is
    reconciled against the provider invoice, out-of-band — not via the admission
    counter that gates whether the NEXT answer is allowed to run.

    http_status is derived from the error class, NOT left NULL: a rate_limit
    books 429 specifically because adaptive_cooldown counts recent
    `http_status = 429` rows to escalate its backoff. With NULL that count was
    always 0, so the exponential step never fired and a per-minute-429 key got
    re-picked every base-cooldown and re-stormed the provider — the exact retry
    storm the adaptive backoff exists to damp (fix 2026-07-10)."""
    kind = classify_provider_error(exc, provider)
    http_status = 429 if kind == "rate_limit" else (401 if kind == "auth" else None)
    await record_usage(
        api_key_id=key.id, project_id=project.id, lease_id=None,
        provider=provider, model=model, capability=capability,
        workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
        latency_ms=None, status="error", error_kind=type(exc).__name__,
        http_status=http_status,
    )


def _wants_json(response_format: dict[str, Any] | None) -> bool:
    return bool(response_format) and response_format.get("type") in (
        "json_object", "json_schema"
    )


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _billed_cost(key: ApiKeyRow, meta: dict[str, Any]) -> float:
    """What we actually owe the provider for this call.

    `estimate_llm_cost` (LiteLLM's pricing map) prices by MODEL — it has no
    concept of "this specific key is on a free plan". A free-tier key calling
    e.g. gemini-2.5-flash gets the same nominal per-token price a paid caller
    would pay, even though the free plan absorbs it at $0 real cost to us.
    Free-tier keys always bill $0; the `tier` column is the source of truth.

    voyage history (2026-07-07): a voyage carve-out here used to bill real
    cost unconditionally, because `voyage-3` had a ZERO free-token allocation
    on our accounts (real invoices arrived while we tracked $0). We have since
    moved the default embedding model to `voyage-4`, which grants 200M free
    tokens/month — genuinely $0 under our ~61M/mo run-rate — so a voyage
    free-tier key is now correctly $0 like any other free key, and the
    carve-out is gone. If a voyage account ever exhausts its 200M monthly free
    allocation, flip that specific key to `tier='paid'` and it bills the real
    per-token cost from then on (the same mechanism every paid key uses).
    """
    return 0.0 if key.tier == "free" else meta["cost_usd"]


@dataclass
class ChatOutcome:
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class _BudgetExhausted:
    """Sentinel run_chat returns when a PROJECT/GLOBAL daily cap is spent — a
    distinct outcome from None (no provider had free capacity). Lets the caller
    give the job an honest 'budget cap' error and stop retrying (more retries
    can't create budget) instead of the misleading 'no provider available'."""
    __slots__ = ()


BUDGET_EXHAUSTED = _BudgetExhausted()


class _Flow(Enum):
    """Verdict of one key attempt — how run_chat's chain walk proceeds."""
    NEXT_KEY = auto()
    NEXT_KEY_EMPTY = auto()  # empty JSON body — retry same provider, bounded by run_chat
    NEXT_PROVIDER = auto()
    BUDGET_EXHAUSTED = auto()  # project/global cap spent — abort the whole walk
    SUCCESS = auto()


async def _handle_call_error(
    *, exc: Exception, key: ApiKeyRow, project: ProjectRow, provider: str,
    use_model: str, capability: str, workflow: str | None,
    est_tokens: int,
) -> _Flow:
    """Classify a failed call_llm, penalize/book it, return the walk verdict."""
    # Model gone/unprovisioned (404) — it's a MODEL problem, not a
    # KEY one: this key's OTHER models still work, and sibling keys
    # of this provider run the same dead model. Do NOT penalize the
    # key; break straight to the next provider. (Interim model-level
    # fix — see is_model_unavailable / roadmap §3.1.)
    if is_model_unavailable(exc):
        await _record_error(
            key=key, project=project, provider=provider,
            model=use_model, capability=capability, workflow=workflow, exc=exc,
        )
        log.warning("provider %s model %s unavailable (%s) — next provider",
                    provider, use_model, type(exc).__name__)
        return _Flow.NEXT_PROVIDER  # not next key of the same dead model
    kind = await _penalize(key, exc)
    # Self-learn the size ceiling: if the provider rejected the
    # prompt for being too big, remember it so we skip this
    # provider for prompts ≥ this size next time (no hardcoded cap).
    if is_too_large_error(exc):
        await record_too_large(provider, est_tokens)
        log.info("learned: %s rejects ~%d tok prompts",
                 provider, est_tokens)
        return _Flow.NEXT_PROVIDER  # bigger keys won't help
    # Every failed attempt books $0 (see _record_error) — a timeout's real
    # upstream spend is reconciled off the provider invoice, NOT charged to the
    # admission cap the owner reserves for answers (fix 2026-07-16). The
    # reservation is fully released in _run_attempt, so an answerless timeout
    # leaves daily_cost_used_usd untouched.
    await _record_error(
        key=key, project=project, provider=provider,
        model=use_model, capability=capability, workflow=workflow, exc=exc,
    )
    log.warning("provider %s key %s failed (%s): %s",
                provider, key.label, kind, exc)
    return _Flow.NEXT_KEY


async def _record_json_miss(
    *, key: ApiKeyRow, project: ProjectRow, provider: str, use_model: str,
    capability: str, workflow: str | None, text: str, meta: dict[str, Any],
) -> _Flow:
    """Book a billed-but-unusable JSON body, return the walk verdict."""
    # An EMPTY/whitespace body is a TRANSIENT provider throttle, not a
    # model JSON defect: DeepSeek's json_object mode intermittently
    # returns a blank string on large prompts under load (verified
    # 2026-07-10 — ~24% on Stepan's 52k-char follow-up prompt, random
    # per call, unrelated to the key). A retry of the SAME provider
    # almost always returns valid JSON, so retry within the provider
    # rather than burning the whole chain. Non-empty-but-malformed is
    # still a MODEL property (cerebras gpt-oss mangles the same prompt
    # on every key) → skip straight to the next provider.
    empty = not (text or "").strip()
    await record_usage(
        api_key_id=key.id, project_id=project.id, lease_id=None,
        provider=provider, model=use_model, capability=capability,
        workflow=workflow, tokens_in=meta["tokens_in"],
        tokens_out=meta["tokens_out"], cost_usd=meta["cost_usd"],
        cache_read_tokens=meta.get("cache_read_tokens", 0),
        cache_write_tokens=meta.get("cache_write_tokens", 0),
        latency_ms=meta["latency_ms"], status="error",
        error_kind="EmptyBody" if empty else "InvalidJSON",
        http_status=200,
    )
    if empty:
        return _Flow.NEXT_KEY_EMPTY  # run_chat bounds this via _MAX_EMPTY_RETRIES
    log.warning("provider %s returned unparseable/empty JSON, next provider", provider)
    return _Flow.NEXT_PROVIDER  # next provider, not next key of the same model


async def _run_attempt(
    *, key: ApiKeyRow, project: ProjectRow, provider: str, use_model: str,
    capability: str, messages: list[dict[str, Any]], model: str | None,
    max_tokens: int, temperature: float, response_format: dict[str, Any] | None,
    workflow: str | None, est_tokens: int, call_timeout: float,
) -> tuple[_Flow, ChatOutcome | None]:
    """One chat key attempt: reserve cost → call → book the result → verdict."""
    # Worst-case cost estimate (assumes the full max_tokens budget is
    # generated) reserved BEFORE the call — see reserve_cost's
    # docstring for why this closes a real concurrent-overspend race
    # that a plain pre-loaded-object comparison couldn't. Free-tier
    # keys always cost $0 (_billed_cost) — never estimate/reserve for
    # them, matching reserve_cost's own free-tier skip.
    estimated_cost = (
        0.0 if key.tier == "free"
        else estimate_llm_cost(use_model, est_tokens, max_tokens)
    )
    try:
        await reserve_cost(api_key=key, project=project, estimated_cost=estimated_cost)
    except CostGuardError as e:
        await audit(actor=f"project:{project.name}", action="cap_block",
                    target=f"provider={provider}", metadata={"reason": str(e)})
        # Book the block in usage_log exactly like every other failed attempt
        # (status=error, error_kind=CapBlock, http 402, $0) — the audit_log alone
        # used to record it, so ~8800 cap-blocked picks in 2h vanished from the
        # usage view and jobs died invisibly as "no provider available" (prod
        # 2026-07-16). 402 Payment Required is the honest, greppable signal.
        await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=use_model, capability=capability,
            workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
            latency_ms=None, status="error", error_kind="CapBlock",
            http_status=402,
        )
        # A project/global cap blocks EVERY paid provider identically, so walking
        # to the next paid key is futile spend — abort the whole walk. A per-key
        # cap is local (other keys/providers may still have room) → next provider.
        if e.kind in ("project", "global"):
            return _Flow.BUDGET_EXHAUSTED, None
        return _Flow.NEXT_PROVIDER, None
    plain = decrypt(key.token_encrypted)
    try:
        text, meta = await call_llm(
            model=use_model, messages=messages, api_key=plain,
            max_tokens=max_tokens, temperature=temperature,
            response_format=response_format,
            extra=extra_for_provider(provider, getattr(key, "account_id", None)),
            timeout=call_timeout,
        )
        meta["cost_usd"] = _billed_cost(key, meta)
    except Exception as e:  # noqa: BLE001 — classify, cool the key, try next
        # Attempt is over (however it ends) — fully release the reservation so
        # an answerless call (incl. a paid timeout) consumes NO admission budget;
        # _record_error books the row at $0.
        await release_cost(api_key=key, estimated_cost=estimated_cost)
        return await _handle_call_error(
            exc=e, key=key, project=project, provider=provider,
            use_model=use_model, capability=capability, workflow=workflow,
            est_tokens=est_tokens,
        ), None

    # Call resolved (successfully) — release the reservation; record_usage
    # below books the REAL final cost (meta["cost_usd"]) on top, so the
    # key ends up debited by exactly the real cost, never the estimate.
    await release_cost(api_key=key, estimated_cost=estimated_cost)

    # Deterministic JSON quality gate: an unparseable JSON body (gemini
    # truncated, deepseek rogue) is billed but treated as a failure.
    if _wants_json(response_format) and not _is_valid_json(text):
        return await _record_json_miss(
            key=key, project=project, provider=provider, use_model=use_model,
            capability=capability, workflow=workflow, text=text, meta=meta,
        ), None

    request_id = await record_usage(
        api_key_id=key.id, project_id=project.id, lease_id=None,
        provider=provider, model=use_model, capability=capability,
        workflow=workflow, tokens_in=meta["tokens_in"],
        tokens_out=meta["tokens_out"], cost_usd=meta["cost_usd"],
        cache_read_tokens=meta.get("cache_read_tokens", 0),
        cache_write_tokens=meta.get("cache_write_tokens", 0),
        latency_ms=meta["latency_ms"], status="ok", error_kind=None,
        http_status=200,
    )
    # Cache deterministic (translate/prefilter) successes for verbatim repeats.
    response_cache.put(capability, messages, text, model=model,
                        max_tokens=max_tokens, temperature=temperature)
    # A success pins this (project, provider) to this key so the NEXT pick
    # lands where the provider-side prompt cache is already warm.
    await note_affinity_shared(project.id, provider, key.id)
    return _Flow.SUCCESS, ChatOutcome(
        text=text, provider=provider, model=meta["model"],
        tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
        cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
        key_label=key.label, request_id=request_id,
        cache_read_tokens=meta.get("cache_read_tokens", 0),
        cache_write_tokens=meta.get("cache_write_tokens", 0),
    )


async def _size_filtered(full_chain: list[str], est_tokens: int, capability: str) -> list[str]:
    # Size-aware provider filter: drop providers whose single-request token
    # ceiling can't fit this prompt (e.g. groq getting a 24k Coach prompt — a
    # guaranteed 413). Ceilings never drop below MIN_LEARNABLE_CEILING, so a
    # smaller prompt fits EVERY provider — skip the learned_ceilings() DB
    # round-trip entirely on the high-volume small-prompt path (chat:fast,
    # translate). Above the floor, filter as before (fall back to the full
    # chain if every provider is size-skipped, so we never starve).
    if est_tokens < MIN_LEARNABLE_CEILING:
        return full_chain
    learned = await learned_ceilings()
    sized_chain = [
        p for p in full_chain if fits_context(p, est_tokens, learned.get(p))
    ]
    if len(sized_chain) < len(full_chain):
        log.info("chat:%s prompt ~%d tok — skipping over-ceiling providers: %s",
                 capability, est_tokens,
                 [p for p in full_chain if p not in sized_chain])
    return sized_chain or full_chain


async def run_chat(
    *,
    project: ProjectRow,
    capability: str,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    response_format: dict[str, Any] | None,
    workflow: str | None,
    paid_only: bool = False,
) -> ChatOutcome | _BudgetExhausted | None:
    """Walk the capability chain; return the first provider that succeeds, else None.

    Within a provider, try up to `_MAX_KEYS_PER_PROVIDER` keys (the selector hands
    out a fresh LRU key each time and `_penalize` cools failed ones) before falling
    through — so one rate-limited free key doesn't sink the whole request.

    `paid_only=True` demands a paid-tier key on every pick (the job queue's
    final-retry escalation): the same chain walk, but free keys are invisible,
    so the request lands on the paid tail or honestly returns None.

    Returns `BUDGET_EXHAUSTED` (not None) when a project/global daily cap is
    spent — the caller can then fail the job honestly instead of masking it as
    "no provider available" and burning retries that can't create budget.
    """
    scope = scope_for(capability)

    # Exact-match cache for deterministic capabilities (translate/prefilter):
    # the same inputs recur verbatim, so a cached answer is correct and skips
    # the whole LLM round-trip. No-op for chat/* (not deterministic).
    cached = response_cache.get(capability, messages, model=model,
                                 max_tokens=max_tokens, temperature=temperature)
    if cached is not None:
        return ChatOutcome(
            text=cached, provider="cache", model="cache",
            tokens_in=0, tokens_out=0, cost_usd=0.0, latency_ms=0,
            key_label="cache", request_id=0,
        )

    est_tokens = estimate_prompt_tokens(messages)
    full_chain = chain_for(capability)
    # JSON requests: try JSON-reliable providers first (gpt-oss/cohere sink to
    # the back) so a structured call doesn't lead with a model that mangles
    # JSON — cuts InvalidJSON at the source, not after the wasted call.
    if _wants_json(response_format):
        full_chain = deprioritize_for_json(full_chain)
    chain = await _size_filtered(full_chain, est_tokens, capability)

    # Dynamic per-request attempt budget = "try every key we have across the
    # whole chain before giving up", so the paid tail is always reached before
    # a 503 (a saturated provider yields no key → 0 attempts, so the chain
    # falls through to it fast). Bounded by the absolute runaway backstop.
    attempt_cap = _attempt_budget(chain)
    call_timeout = _call_timeout(capability)
    require_tier = "paid" if paid_only else None
    attempts = 0
    for provider in chain:
        empty_retries = 0  # bounded per provider — see the NEXT_KEY_EMPTY branch below
        for _ in range(_max_keys(provider)):
            if attempts >= attempt_cap:
                log.warning("chat:%s hit per-request attempt cap (%d) — 503",
                            capability, attempt_cap)
                return None
            key = await pick_and_reserve(provider, scope=scope,
                                          require_tier=require_tier,
                                          project_id=project.id)
            if key is None:
                break  # no (more) available key for this provider → next provider
            attempts += 1
            use_model = model or model_for(provider, capability)
            if not use_model:
                break  # provider can't serve this capability → next provider
            flow, outcome = await _run_attempt(
                key=key, project=project, provider=provider, use_model=use_model,
                capability=capability, messages=messages, model=model,
                max_tokens=max_tokens, temperature=temperature,
                response_format=response_format, workflow=workflow,
                est_tokens=est_tokens, call_timeout=call_timeout,
            )
            if flow is _Flow.SUCCESS:
                return outcome
            if flow is _Flow.BUDGET_EXHAUSTED:
                log.warning("chat:%s aborted — project/global daily cap reached",
                            capability)
                return BUDGET_EXHAUSTED
            if flow is _Flow.NEXT_KEY_EMPTY:
                if empty_retries < _MAX_EMPTY_RETRIES:
                    # Retry the SAME provider's next key ONCE — that rescues a
                    # transient throttle. But cap it: some prompts make DeepSeek's
                    # json_object return empty DETERMINISTICALLY (verified: a
                    # 30k-char system prompt is empty on every key/call), and
                    # retrying every key there just burns the whole provider for
                    # nothing. After the cap, treat it like any other JSON miss and
                    # move to the next provider.
                    empty_retries += 1
                    log.warning("provider %s returned empty body — retrying next key", provider)
                    continue
                log.warning("provider %s returned unparseable/empty JSON, next provider", provider)
                break  # next provider, not next key of the same model
            if flow is _Flow.NEXT_PROVIDER:
                break
            # _Flow.NEXT_KEY — walk to this provider's next key
    return None


@dataclass
class EmbedOutcome:
    embeddings: list[list[float]]
    provider: str
    model: str
    tokens_in: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int


class EmbedFailed(Exception):
    """Every key of `provider` failed — route maps this to HTTP 502."""


async def _handle_attempt_failure(
    *, key: ApiKeyRow, project: ProjectRow, provider: str, model: str,
    capability: str, workflow: str | None, exc: Exception,
) -> None:
    """Shared embed/transcribe failure tail: penalize the key, book the error row."""
    await _penalize(key, exc)
    await _record_error(
        key=key, project=project, provider=provider, model=model,
        capability=capability, workflow=workflow, exc=exc,
    )


async def run_embed(
    *,
    project: ProjectRow,
    provider: str,
    inputs: list[str],
    model: str | None,
    workflow: str | None,
) -> EmbedOutcome | None:
    """Embed `inputs` via `provider`, retrying up to `_max_keys(provider)` keys
    of that SAME provider on failure. None → no key at all (503); EmbedFailed →
    every key tried and failed (502).

    Deliberately does NOT fall back to a different provider (unlike
    run_chat/run_transcribe walking their capability chain): voyage-3 and
    cohere embed-english-v3 are different vector spaces with no guaranteed
    cross-compatible dimensionality. Silently switching provider mid-batch
    would poison a vector index with incomparable embeddings. `provider` is
    the caller's explicit choice — the broker only rotates KEYS within it.

    (Real-world driver: voyage APIConnectionError — 100% of 7d embedding
    failures — is a transient network blip, not a bad key or a dead
    provider; a fresh key retry turns most of these into a normal success.)
    """
    use_model = model or model_for(provider, "embedding") or "voyage/voyage-4"
    any_key_seen = False
    last_exc: Exception | None = None
    for _ in range(_max_keys(provider)):
        key = await pick_and_reserve(provider, scope=scope_for("embedding"),
                                      project_id=project.id)
        if key is None:
            break  # no (more) available key for this provider
        any_key_seen = True
        plain = decrypt(key.token_encrypted)
        try:
            vectors, meta = await embed(model=use_model, texts=inputs, api_key=plain)
            meta["cost_usd"] = _billed_cost(key, meta)
        except Exception as e:  # noqa: BLE001 — classify, cool the key, try next
            last_exc = e
            await _handle_attempt_failure(
                key=key, project=project, provider=provider,
                model=use_model, capability="embedding", workflow=workflow, exc=e,
            )
            log.warning("provider %s key %s embed failed, trying next key: %s",
                        provider, key.label, e)
            continue
        request_id = await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=use_model, capability="embedding",
            workflow=workflow, tokens_in=meta["tokens_in"], tokens_out=0,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            status="ok", error_kind=None, http_status=200,
        )
        await note_affinity_shared(project.id, provider, key.id)
        return EmbedOutcome(
            embeddings=vectors, provider=provider, model=use_model,
            tokens_in=meta["tokens_in"], cost_usd=meta["cost_usd"],
            latency_ms=meta["latency_ms"], key_label=key.label,
            request_id=request_id,
        )
    if not any_key_seen:
        return None
    raise EmbedFailed(str(last_exc) if last_exc else "all keys failed")


@dataclass
class TranscribeOutcome:
    text: str
    provider: str
    model: str
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int


class TranscribeFailed(Exception):
    """All transcription providers in the chain failed — route maps to 502."""


async def run_transcribe(
    *,
    project: ProjectRow,
    audio: bytes,
    filename: str,
    workflow: str | None,
) -> TranscribeOutcome | None:
    """Audio → text, walking the 'transcription' chain (groq → openai).

    None → no key anywhere (503); TranscribeFailed → every provider errored (502).
    """
    scope = scope_for("transcription")
    last_exc: Exception | None = None
    any_key_seen = False

    for provider in chain_for("transcription"):
        key = await pick_and_reserve(provider, scope=scope,
                                      project_id=project.id)
        if key is None:
            continue
        any_key_seen = True
        use_model = model_for(provider, "transcription")
        if not use_model:
            continue
        plain = decrypt(key.token_encrypted)
        try:
            text, meta = await transcribe(
                model=use_model, audio=audio, filename=filename, api_key=plain,
            )
            meta["cost_usd"] = _billed_cost(key, meta)
        except Exception as e:
            last_exc = e
            await _handle_attempt_failure(
                key=key, project=project, provider=provider,
                model=use_model, capability="transcription", workflow=workflow, exc=e,
            )
            continue
        request_id = await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=use_model, capability="transcription",
            workflow=workflow, tokens_in=0, tokens_out=0,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            status="ok", error_kind=None, http_status=200,
        )
        await note_affinity_shared(project.id, provider, key.id)
        return TranscribeOutcome(
            text=text, provider=provider, model=use_model,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            key_label=key.label, request_id=request_id,
        )

    if not any_key_seen:
        return None
    raise TranscribeFailed(str(last_exc) if last_exc else "all providers failed")
