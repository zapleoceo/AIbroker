"""Chat / embed / decision / transcribe orchestration — the WALKS.

Routes stay thin (validate → call → shape response). This module decides WHICH
(provider, model, key) to try next and when to stop; what happens to a single
key attempt (reserve → call → release → gate → record → affinity, with one error
path) is services/attempt.py, shared by every capability. Dated tuning history
for the constants below: docs/history/llm-service-tuning.md.
"""
from __future__ import annotations

import asyncio  # noqa: F401 — kept for callers patching svc.asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from aibroker.db.models import ProjectRow
from aibroker.providers.adapters import extra_for_provider, is_deepseek_big_json_prompt
from aibroker.providers.catalog import PinTarget, UnknownModel, resolve_pin
from aibroker.providers.context_limits import (
    MIN_LEARNABLE_CEILING,
    estimate_prompt_tokens,
    fits_context,
)
from aibroker.providers.cost import estimate_llm_cost, estimate_transcription_cost
from aibroker.providers.decisions import (
    DECISION_FALLBACK_MODEL,
    JEV_INPUT_USD_PER_TOKEN,
    estimate_tokens,
)
from aibroker.providers.observations import learned_ceilings
from aibroker.providers.peak_pricing import peak_multiplier
from aibroker.providers.prompt_cache import cache_key_for

# Re-exported: tests and services/__init__ import classify_provider_error from here.
from aibroker.providers.provider_errors import classify_provider_error
from aibroker.providers.registry import (
    max_keys_for,
    model_for,
    paid_providers,
    rotation_for,
    spec_or_default,
)
from aibroker.providers.transport import call_llm, decide, embed, transcribe, transport_for
from aibroker.routing import (
    CostGuardError,
    affinity,
    chain_for,
    circuit,
    deprioritize_deepseek_for_savings,
    deprioritize_for_json,
    pick_and_reserve,
    scope_for,
)
from aibroker.routing.chains import free_first_walk
from aibroker.routing.model_cooldown import cooled_models
from aibroker.services import response_cache
from aibroker.services.attempt import Attempt, AttemptResult, Flow, Rejection, run_attempt
from aibroker.services.tool_contract import TOOL_PROVIDERS, tool_model_provider, validate_result

__all__ = [
    "BUDGET_EXHAUSTED", "CONTENT_FAILED", "ChatOutcome", "DecisionFailed", "DecisionOutcome", "EmbedFailed",
    "EmbedOutcome", "EmbedRequestInvalid", "TranscribeFailed", "TranscribeOutcome", "classify_provider_error",
    "run_chat", "run_decision", "run_embed", "run_transcribe",
]

log = logging.getLogger(__name__)

# Keys tried per provider before the walk moves on: ProviderSpec.max_keys
# (default 5; gemini/cerebras 3 — they rate-limit their keys in lockstep).
# EmptyBody / InvalidJSON are properties of the (model, prompt) pair, not of the
# key: after the first one a request moves to the NEXT provider instead of trying
# the sibling keys of the same model (2026-10-04: one 39k-token Stepan prompt burned
# ~36 attempts over 13 min, 3-4 deepseek keys per walk, every one empty). See
# docs/routing.md "Content failures".
# Failure kinds a quality gate raises for a deterministic bad answer.
_CONTENT_FAILURES = frozenset({"EmptyBody", "InvalidJSON"})

# Distinct keys of one provider that must return an empty body inside the breaker
# window before we try the free tier ahead of it (one flaky key must not reorder).
_EMPTY_STORM_MIN_KEYS = 2

# Absolute runaway backstop on provider-call attempts per request; the real budget
# is the sum of per-provider key allowances over the chain (see `_attempt_budget`)
# and this must stay ABOVE the longest real chain's key sum.
_MAX_ATTEMPTS_ABS = 100

# Per-provider-call timeout (seconds): a safety net against a hung upstream, not a
# latency budget. chat:deep legitimately runs minutes, so it gets a long ceiling
# that still fires before the job queue's 25-min stale-reclaim window.
_CALL_TIMEOUT_S = 60.0
_DEEP_CALL_TIMEOUT_S = 19 * 60.0

# Overall wall-clock budget for a NON-deep walk, kept under job_queue's 25-min
# reclaim so a slow storm walk finishes before a second worker could re-execute it.
_CHAT_WALL_DEADLINE_S = 18 * 60.0

# chat:deep's single call is ~19 min, so only fast key rotation in the first
# minutes is allowed (5 + 19 = 24 < 25).
_DEEP_WALL_DEADLINE_S = 5 * 60.0

# The gate is a FINISH-BY deadline: "can this attempt finish in time" (now + its
# call timeout <= finish_by), not "may it start" — a longer timeout or a second
# local key can then never create a double-execution.
_DEEP_FINISH_BY_S = _DEEP_WALL_DEADLINE_S + _DEEP_CALL_TIMEOUT_S

_CAP_MESSAGE = "daily budget cap reached — retry after 00:00 UTC"


def _now() -> float:
    """Monotonic clock — indirected so the wall-clock gate is unit-testable."""
    return time.monotonic()


def _max_keys(provider: str) -> int:
    return max_keys_for(provider)


def _attempt_budget(chain: list[str]) -> int:
    """Total attempts allowed over `chain`: the sum of every provider's key
    allowance, bounded by the runaway backstop — so each provider (incl. the paid
    tail) is reachable before a 503; a saturated provider costs 0 attempts."""
    return min(_MAX_ATTEMPTS_ABS, sum(_max_keys(p) for p in chain))


def _call_timeout(capability: str, provider: str | None = None) -> float:
    """Per-call ceiling: chat:deep gets the long rope; otherwise the provider's
    transport may ask for its own (self-hosted vision runs minutes on CPU and
    cannot rack up a bill by being slow); default 60s."""
    if capability == "chat:deep":
        return _DEEP_CALL_TIMEOUT_S
    model = model_for(provider, capability) if provider else None
    own = transport_for(model).call_timeout(capability) if model else None
    return own if own is not None else _CALL_TIMEOUT_S


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


# ─── Outcomes ───────────────────────────────────────────────────────────────


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
    # The EXACT model that answered when it says more than `model` (the routing
    # name) does — see providers/model_identity.py. None when they are the same.
    model_served: str | None = None
    # Vision extras: populated only by the self-hosted `local` provider, which
    # classifies the image on the same pass; `text` keeps its meaning for EVERY
    # provider so a mid-chain fallback cannot change the response shape.
    vision_type: str | None = None
    vision_format: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    finish_reason: str | None = None
    refusal: str | None = None


class _BudgetExhausted:
    """Sentinel run_chat returns when a PROJECT/GLOBAL daily cap is spent — a
    distinct outcome from None (no provider had capacity), so the caller can fail
    the job honestly and stop retrying (more retries can't create budget)."""
    __slots__ = ()


BUDGET_EXHAUSTED = _BudgetExhausted()


class _ContentFailed:
    """Sentinel run_chat returns when EVERY attempt of the walk got a billed,
    deterministic bad answer (EmptyBody / InvalidJSON) and nothing else went wrong
    (no rate limit, timeout, 5xx, capped key). The prompt, not capacity, is the
    problem, so the job queue gives it one more try at most instead of eight."""
    __slots__ = ()


CONTENT_FAILED = _ContentFailed()


# ─── Chat ───────────────────────────────────────────────────────────────────


def _chat_gate(
    *, provider: str, key_id: int, response_format: dict[str, Any] | None,
    tools: list[dict[str, Any]] | None, tool_choice: Any,
):
    """The chat quality gate: native-tool contract, untrusted-empty providers, and
    the deterministic JSON check (an unparseable body is billed but a failure)."""
    spec = spec_or_default(provider)

    def check(text: str, meta: dict[str, Any]) -> Rejection | None:
        if tools and (failure := validate_result(text, meta, tools, tool_choice)):
            return Rejection(failure, Flow.NEXT_PROVIDER, http_status=502)
        if spec.empty_is_failure and not (text or "").strip():
            # Never a real answer (a 4B model on CPU produced nothing): escalate to
            # the next PROVIDER — re-asking a one-key local provider is deterministic.
            return Rejection("EmptyBody", Flow.NEXT_PROVIDER, http_status=502, bill=False,
                             log_message=f"{provider} returned an empty body — escalating")
        if _wants_json(response_format) and not _is_valid_json(text):
            # Empty and malformed are both a (model, prompt) property: the next
            # provider, never a sibling key of this model.
            if not (text or "").strip():
                return Rejection(
                    "EmptyBody", Flow.NEXT_PROVIDER,
                    note=lambda: circuit.note_empty_body(provider, key_id),
                    log_message=f"provider {provider} returned an empty JSON body, next provider")
            return Rejection(
                "InvalidJSON", Flow.NEXT_PROVIDER,
                log_message=f"provider {provider} returned unparseable JSON, next provider")
        return None

    return check


async def _size_filtered(full_chain: list[str], est_tokens: int, capability: str) -> list[str]:
    # Drop providers whose single-request ceiling can't fit this prompt (a
    # guaranteed 413). Ceilings never drop below MIN_LEARNABLE_CEILING, so a smaller
    # prompt fits EVERY provider — skip the DB round-trip on the small-prompt path.
    # Falls back to the full chain if every provider is size-skipped.
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


@dataclass(frozen=True)
class _Step:
    """One leg of a walk: a provider, optionally a fixed model (a pin) and an
    optional key-tier restriction (free pass / paid pass of a pinned walk)."""
    provider: str
    model: str | None = None
    tier: str | None = None
    key_id: int | None = None      # route-affinity leg: exactly this key, one attempt
    free_only: bool = False        # affinity leg must not spend where free-first walks


def _pinned_steps(targets: list[PinTarget], *, paid_only: bool) -> list[_Step]:
    """Deterministic walk for a pinned model: FREE-tier keys of every candidate
    first (providers in fixed rank order), then paid keys — never shuffled."""
    paid = paid_providers()
    free_pass = [] if paid_only else [
        _Step(t.provider, t.model, "free") for t in targets if t.provider not in paid]
    return free_pass + [_Step(t.provider, t.model, "paid") for t in targets]


async def _with_affinity(
    steps: list[_Step], project_id: int, workflow: str | None, capability: str,
    pinned: str | None, *, paid_only: bool,
) -> list[_Step]:
    """Put the request family's affine (provider, model, key) in front of the walk.

    The leg is a single attempt on exactly that key; when it is cooling / capped /
    errored the normal walk follows and the success re-pins (see
    routing/affinity.py). Only a target the walk would allow anyway is promoted —
    it must still be one of the shaped steps (tools / size / JSON / pin filters
    already applied) — and a paid target never jumps a free head."""
    if not steps:
        return steps
    target = await affinity.lookup_route(project_id, workflow, capability, pinned)
    if target is None or not affinity.may_promote(target, steps[0].provider,
                                                  paid_only=paid_only):
        return steps
    if not any(s.provider == target.provider and (not s.model or s.model == target.model)
               for s in steps):
        return steps
    free_head = not paid_only and steps[0].provider not in paid_providers()
    return [_Step(target.provider, target.model, "paid" if paid_only else None,
                  key_id=target.key_id, free_only=free_head), *steps]


def _shape_chain(
    chain: list[str], *, capability: str, paid_only: bool,
    response_format: dict[str, Any] | None, messages: list[dict[str, Any]],
    at: datetime | None,
) -> list[str]:
    """Order the capability chain for this request (free-first walk, JSON
    reliability, deepseek savings)."""
    chain = free_first_walk(capability, chain, paid_only=paid_only)
    # JSON requests: JSON-reliable providers first, incapable ones dropped — cuts
    # InvalidJSON at the source, not after the wasted call.
    if _wants_json(response_format):
        chain = deprioritize_for_json(chain)
    # Savings: chat:smart anchors on deepseek, but not during its peak-pricing hours
    # (2x), on a big-JSON prompt that empties its body, or in a provider-side
    # empty-body storm — free providers get first shot and deepseek stays the
    # fallback (deferral, not a skip).
    should_defer_deepseek = (
        peak_multiplier("deepseek", at) > 1.0
        or is_deepseek_big_json_prompt(response_format, messages)
        or "deepseek" in circuit.providers_in_empty_storm(_EMPTY_STORM_MIN_KEYS)
    )
    return deprioritize_deepseek_for_savings(chain, should_defer=should_defer_deepseek)


def _chat_outcome(provider: str, key_label: str, res: AttemptResult) -> ChatOutcome:
    meta = res.meta
    return ChatOutcome(
        text=res.payload, provider=provider, model=meta["model"],
        model_served=meta.get("model_served"),
        tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
        cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
        key_label=key_label, request_id=res.usage_id or 0,
        cache_read_tokens=meta.get("cache_read_tokens", 0),
        cache_write_tokens=meta.get("cache_write_tokens", 0),
        vision_type=meta.get("vision_type"), vision_format=meta.get("vision_format"),
        tool_calls=meta.get("tool_calls"), finish_reason=meta.get("finish_reason"),
        refusal=meta.get("refusal"),
    )


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
    at: datetime | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> ChatOutcome | _BudgetExhausted | _ContentFailed | None:
    """Walk the capability chain; return the first provider that succeeds, else None.

    `model` pins EXACTLY that model (a routing id or a bare canonical name — see
    providers/catalog.resolve_pin; UnknownModel if it is not in the catalog): the
    walk is then deterministic — free-tier keys of every provider serving it first,
    then paid keys, providers in fixed rank order, no rotation, no reshuffling.

    `at` overrides the clock for deepseek's peak-hour check (tests pin it).
    `paid_only=True` demands a paid-tier key on every pick (the job queue's
    final-retry escalation). Returns `BUDGET_EXHAUSTED` (not None) when a
    project/global daily cap is spent, so the job fails honestly instead of
    burning retries that cannot create budget. Returns `CONTENT_FAILED` when every
    attempt was an EmptyBody / InvalidJSON and nothing transient happened.
    """
    pinned_tool_provider = tool_model_provider(model) if tools else None
    scope = scope_for(capability)

    # Exact-match cache for deterministic capabilities (translate/prefilter).
    cached = (None if tools else response_cache.get(capability, messages, model=model,
                                 max_tokens=max_tokens, temperature=temperature,
                                 project_id=project.id,
                                 response_format=response_format))
    if cached is not None:
        return ChatOutcome(
            text=cached, provider="cache", model="cache",
            tokens_in=0, tokens_out=0, cost_usd=0.0, latency_ms=0,
            key_label="cache", request_id=0,
        )

    est_tokens = estimate_prompt_tokens(messages)
    if model:
        targets = resolve_pin(model, capability, chain_for(capability))
        if tools:
            targets = [t for t in targets if t.provider in TOOL_PROVIDERS
                       and (pinned_tool_provider is None or t.provider == pinned_tool_provider)]
        if _wants_json(response_format):
            capable = set(deprioritize_for_json([t.provider for t in targets]))
            targets = [t for t in targets if t.provider in capable]
        wanted = await _size_filtered([t.provider for t in targets], est_tokens, capability)
        steps = _pinned_steps([t for t in targets if t.provider in wanted], paid_only=paid_only)
    else:
        chain = _shape_chain(chain_for(capability), capability=capability,
                             paid_only=paid_only, response_format=response_format,
                             messages=messages, at=at)
        if tools:
            chain = [p for p in chain if p in TOOL_PROVIDERS
                     and (pinned_tool_provider is None or p == pinned_tool_provider)]
        chain = await _size_filtered(chain, est_tokens, capability)
        steps = [_Step(p) for p in chain]
    if tools:
        est_tokens += len(json.dumps(tools, ensure_ascii=False)) // 3 + 1

    steps = await _with_affinity(steps, project.id, workflow, capability, model,
                                 paid_only=paid_only)
    attempt_cap = _attempt_budget([s.provider for s in steps])
    base_tier = "paid" if paid_only else None
    degraded_free = False   # a spent paid budget downgrades the REST of the walk
    # Every capability gets a wall-clock deadline so a storm walk can't outlast the
    # job's stale-reclaim window and get re-executed by another worker.
    finish_by = _now() + (
        _DEEP_FINISH_BY_S if capability == "chat:deep" else _CHAT_WALL_DEADLINE_S)
    attempts = 0
    content_failures = 0     # attempts that came back EmptyBody / InvalidJSON
    other_failures = False   # any failure that a later retry could plausibly cure
    for step in steps:
        provider = step.provider
        require_tier = "free" if (degraded_free and step.tier is None) else (
            step.tier or base_tier)
        # Starting offset into the provider's model pool, fixed ONCE from the first
        # key we get: varying it per key id spreads which model burns its daily
        # quota first, then advancing by attempt_in_provider guarantees consecutive
        # attempts hit DIFFERENT models.
        rotation_base: int | None = None
        for attempt_in_provider in range(1 if step.key_id else _max_keys(provider)):
            if attempts >= attempt_cap:
                log.warning("chat:%s hit per-request attempt cap (%d) — 503",
                            capability, attempt_cap)
                return None
            call_timeout = _call_timeout(capability, provider)
            if _now() + call_timeout > finish_by:
                log.warning("chat:%s — a %ds %s attempt would end past the "
                            "finish-by deadline mid-walk; stop starting attempts "
                            "so the job finishes before stale-reclaim "
                            "(no double-execution)",
                            capability, int(call_timeout), provider)
                return None
            primary = model_for(provider, capability)
            pool = _model_pool(provider, capability, primary, step.model)
            key = await pick_and_reserve(provider, scope=scope,
                                          require_tier=require_tier,
                                          project_id=project.id,
                                          models=pool or None,
                                          **({"only_key_id": step.key_id}
                                             if step.key_id else {}))
            if key is not None and step.free_only and key.tier != "free":
                key = None   # free-first is policy: a warm paid cache is not worth a bill
            if key is None:
                break  # no (more) available key for this provider → next step
            attempts += 1
            # Rotate across the provider's models instead of hammering one: Google
            # meters its free tier PER MODEL per key, so a 429 on the primary says
            # nothing about the others. Offsetting by key.id and attempt spreads the
            # load and stays deterministic per key. A PIN never rotates.
            if step.model:
                use_model = step.model
            elif primary is None:
                use_model = None
            else:
                if rotation_base is None:
                    rotation_base = key.id
                use_model = await _rotate_model(
                    pool, key.id, (rotation_base + attempt_in_provider) % len(pool))
            if not use_model:
                break  # provider can't serve this capability → next step
            ck = cache_key_for(project.id, workflow or "", capability, model or "")
            res = await run_attempt(Attempt(
                key=key, project=project, provider=provider, model=use_model,
                capability=capability, workflow=workflow, est_tokens=est_tokens,
                # Worst-case cost (full max_tokens generated) reserved BEFORE the
                # call; free-tier keys never estimate/reserve.
                estimated_cost=(0.0 if key.tier == "free"
                                else estimate_llm_cost(use_model, est_tokens, max_tokens)),
                call=_chat_call(
                    provider=provider, key=key, model=use_model, messages=messages,
                    max_tokens=max_tokens, temperature=temperature,
                    response_format=response_format, capability=capability,
                    timeout=call_timeout, tools=tools, tool_choice=tool_choice,
                    cache_key=ck),
                check=_chat_gate(provider=provider, key_id=key.id,
                                 response_format=response_format, tools=tools,
                                 tool_choice=tool_choice),
                pinned_model=model,
            ))
            flow = res.flow
            if flow is not Flow.SUCCESS:
                if res.rejection in _CONTENT_FAILURES:
                    content_failures += 1
                else:
                    other_failures = True
            if flow is Flow.SUCCESS:
                # Cache deterministic (translate/prefilter) successes for repeats.
                if not tools:
                    response_cache.put(capability, messages, res.payload, model=model,
                                        max_tokens=max_tokens, temperature=temperature,
                                        project_id=project.id,
                                        response_format=response_format)
                return _chat_outcome(provider, key.label, res)
            if flow is Flow.BUDGET_EXHAUSTED:
                if paid_only:
                    log.warning("chat:%s — paid tail budget-capped, no free "
                                "fallback (final retry)", capability)
                    return BUDGET_EXHAUSTED
                if step.tier is not None:
                    # Pinned walk: the free pass already ran before the paid one.
                    return BUDGET_EXHAUSTED
                # A project/global COST cap blocks only PAID keys — $0 free keys are
                # exempt — so a cap-block must NOT abort the walk: downgrade to
                # free-only for the rest of it and retry THIS provider free-only
                # (gemini is MIXED: 1 paid + 7 free keys; the same loop is bounded by
                # _max_keys and pick_and_reserve now filters to free).
                if require_tier != "free":
                    require_tier = "free"
                    degraded_free = True
                    log.info("chat:%s paid budget-capped — walking free-only tail",
                             capability)
                    continue
                break
            if flow is Flow.NEXT_PROVIDER:
                break
            # Flow.NEXT_KEY — walk to this provider's next key
    if content_failures and not other_failures:
        return CONTENT_FAILED
    return None


def _model_pool(provider: str, capability: str, primary: str | None,
                pinned: str | None) -> list[str]:
    """Every model a chat attempt on `provider` may use: the caller's pin alone,
    else primary + rotation extras ([] when the provider has no model)."""
    if pinned:
        return [pinned]
    if primary is None:
        return []
    return [primary, *(m for m in rotation_for(provider, capability) if m != primary)]


async def _rotate_model(pool: list[str], api_key_id: int, start: int) -> str:
    """pool[start], advanced past models this key has cooling down (per-model
    daily quota). One DB read, only for a multi-model pool; fails open — a
    cooldown lookup error must never block a request."""
    if len(pool) < 2:
        return pool[start]
    try:
        cooled = await cooled_models(api_key_id)
    except Exception:  # noqa: BLE001 — fail open
        log.debug("cooled_models lookup failed", exc_info=True)
        return pool[start]
    for i in range(len(pool)):
        candidate = pool[(start + i) % len(pool)]
        if candidate not in cooled:
            return candidate
    return pool[start]


def _chat_call(*, provider: str, key, model: str, messages: list[dict[str, Any]],
               max_tokens: int, temperature: float,
               response_format: dict[str, Any] | None, capability: str,
               timeout: float, tools: list[dict[str, Any]] | None, tool_choice: Any,
               cache_key: str):
    async def call(plain: str):
        return await call_llm(
            model=model, messages=messages, api_key=plain,
            max_tokens=max_tokens, temperature=temperature,
            response_format=response_format,
            extra=extra_for_provider(provider, getattr(key, "account_id", None)),
            timeout=timeout, capability=capability, cache_key=cache_key,
            **({"tools": tools, "tool_choice": tool_choice} if tools else {}),
        )
    return call


# ─── Embedding ──────────────────────────────────────────────────────────────


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
    model_served: str | None = None


class EmbedFailed(Exception):
    """Every key of `provider` failed — route maps this to HTTP 502."""


class EmbedRequestInvalid(ValueError):
    """The request itself is wrong (e.g. a model that belongs to another
    provider) — route maps this to HTTP 400. Nothing was sent anywhere."""


async def run_embed(
    *,
    project: ProjectRow,
    provider: str,
    inputs: list[str],
    model: str | None,
    workflow: str | None,
) -> EmbedOutcome | None:
    """Embed `inputs` via `provider`, retrying up to `max_keys(provider)` keys of
    that SAME provider. None → no key at all (503); EmbedFailed → every key tried
    and failed (502).

    Deliberately does NOT fall back to another provider: voyage and cohere embed
    into different vector spaces, so silently switching mid-batch would poison a
    vector index. `provider` is the caller's explicit choice — the broker only
    rotates KEYS within it. (Voyage APIConnectionError is a transient blip: a
    fresh key retry turns most of them into a success.)
    """
    # Same guard chat has had since 2026-09-26: LiteLLM routes by the model's OWN
    # prefix, so `?provider=voyage` + `model="cohere/embed-..."` would send a
    # voyage key to cohere (a 401 -> mark_dead on a healthy key). The catalog
    # resolves the pin against [provider] only, so a foreign model is rejected here.
    if model:
        try:
            use_model = resolve_pin(model, "embedding", [provider])[0].model
        except UnknownModel as e:
            raise EmbedRequestInvalid(str(e)) from e
    else:
        use_model = model_for(provider, "embedding") or "voyage/voyage-4"
    any_key_seen = False
    last_error: str | None = None
    for _ in range(_max_keys(provider)):
        key = await pick_and_reserve(provider, scope=scope_for("embedding"),
                                      project_id=project.id, models=[use_model])
        if key is None:
            break  # no (more) available key for this provider
        any_key_seen = True
        res = await run_attempt(Attempt(
            key=key, project=project, provider=provider, model=use_model,
            capability="embedding", workflow=workflow,
            estimated_cost=(0.0 if key.tier == "free" else
                            estimate_llm_cost(use_model, sum(len(t) for t in inputs) // 4, 0)),
            call=_embed_call(use_model, inputs),
            cap_flow=Flow.NEXT_KEY,    # a key's own cap — a sibling may have room
            pinned_model=model,
        ))
        if res.flow is Flow.SUCCESS:
            vectors, meta = res.payload, res.meta
            return EmbedOutcome(
                model_served=meta.get("model_served"),
                embeddings=vectors, provider=provider, model=use_model,
                tokens_in=meta["tokens_in"], cost_usd=meta["cost_usd"],
                latency_ms=meta["latency_ms"], key_label=key.label,
                request_id=res.usage_id or 0,
            )
        last_error = _failure_text(res)
        if res.flow is Flow.BUDGET_EXHAUSTED:
            raise EmbedFailed(_CAP_MESSAGE)
        if res.flow is Flow.NEXT_PROVIDER:
            break  # model gone / too large: the other keys would fail identically
    if not any_key_seen:
        return None
    raise EmbedFailed(last_error or "all keys failed")


def _embed_call(model: str, inputs: list[str]):
    async def call(plain: str):
        return await embed(model=model, texts=inputs, api_key=plain)
    return call


def _failure_text(res: AttemptResult) -> str:
    """Message for a failed attempt: the cap message for any cap block, else the
    provider error text."""
    if isinstance(res.error, CostGuardError):
        return _CAP_MESSAGE
    return str(res.error) if res.error else "attempt failed"


# ─── Typed decisions ────────────────────────────────────────────────────────


@dataclass
class DecisionOutcome:
    answers: dict[str, Any]
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int
    model_served: str | None = None


class DecisionFailed(Exception):
    """Every paid decisions key failed — route maps this to HTTP 502."""


async def run_decision(
    *,
    project: ProjectRow,
    state: str,
    questions: dict[str, Any],
    model: str | None,
    workflow: str | None,
) -> DecisionOutcome | None:
    """Typed decisions via OpenRouter, rotating PAID keys only.

    None → no paid key carries llm:decision (503); DecisionFailed → every key
    tried and failed (502). Paid-only so the spend lands on the account that holds
    the prepaid credit and its spend limit (a $0 free-tier key is NOT refused by
    this model, so routing billed traffic there would spread it where no limit is
    set). The reservation is sized from our own per-token price — litellm has no
    entry for this model and would reserve $0.

    Fallback: when the caller did not pin `model` and the primary attempt on a key
    fails (provider error or cap block), the free DECISION_FALLBACK_MODEL is tried
    on that same key, unreserved ($0) and never penalizing the key. A pinned model
    never falls back.
    """
    provider = "openrouter"
    capability = "decision"
    use_model = (resolve_pin(model, capability, [provider])[0].model if model
                 else model_for(provider, capability) or "openrouter/typesafe/jev-1.13")
    use_fallback = model is None and use_model != DECISION_FALLBACK_MODEL
    estimated_cost = estimate_tokens(state, questions) * JEV_INPUT_USD_PER_TOKEN
    any_key_seen = False
    last_error: str | None = None
    for _ in range(_max_keys(provider)):
        key = await pick_and_reserve(provider, scope=scope_for(capability),
                                      require_tier="paid", project_id=project.id)
        if key is None:
            break
        any_key_seen = True
        res = await run_attempt(Attempt(
            key=key, project=project, provider=provider, model=use_model,
            capability=capability, workflow=workflow, estimated_cost=estimated_cost,
            call=_decide_call(use_model, state, questions),
            cap_flow=Flow.NEXT_KEY, pinned_model=model,
        ))
        used_model = use_model
        last_error = _failure_text(res) if res.flow is not Flow.SUCCESS else last_error
        if res.flow is not Flow.SUCCESS and use_fallback:
            # Same key: we already hold it (it may be cooled in the DB after the
            # failure above). $0 by construction, so reserve=False — a spent cap
            # must not refuse a free call.
            fb = await run_attempt(Attempt(
                key=key, project=project, provider=provider,
                model=DECISION_FALLBACK_MODEL, capability=capability,
                workflow=workflow,
                call=_decide_call(DECISION_FALLBACK_MODEL, state, questions),
                reserve=False, penalize=False,
            ))
            if fb.flow is Flow.SUCCESS:
                log.warning("provider %s key %s: %s unavailable (%s), answered by %s",
                            provider, key.label, use_model, last_error,
                            DECISION_FALLBACK_MODEL)
                res, used_model = fb, DECISION_FALLBACK_MODEL
            else:
                last_error = _failure_text(fb)
        if res.flow is not Flow.SUCCESS:
            if res.flow is Flow.BUDGET_EXHAUSTED:
                raise DecisionFailed(_CAP_MESSAGE)
            continue
        meta = res.meta
        return DecisionOutcome(
            answers=res.payload, provider=provider, model=used_model,
            tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            key_label=key.label, request_id=res.usage_id or 0,
            model_served=meta.get("model_served"),
        )
    if not any_key_seen:
        return None
    raise DecisionFailed(last_error or "all keys failed")


def _decide_call(model: str, state: str, questions: dict[str, Any]):
    async def call(plain: str):
        return await decide(model=model, state=state, questions=questions, api_key=plain)
    return call


# ─── Transcription ──────────────────────────────────────────────────────────


@dataclass
class TranscribeOutcome:
    text: str
    provider: str
    model: str
    cost_usd: float
    latency_ms: int
    key_label: str
    request_id: int
    model_served: str | None = None


class TranscribeFailed(Exception):
    """All transcription providers in the chain failed — route maps to 502."""


_LOCAL_ASR_CORRECTION_MAX_TOKENS = 800
_LOCAL_ASR_CORRECTION_TOKEN_CAP = 4000
_LOCAL_ASR_CORRECTION_MIN_KEEP_RATIO = 0.6
_LOCAL_ASR_CORRECTION_PROMPT = (
    "The text below is a raw speech-to-text transcript from a small local ASR "
    "model and may contain misheard words, missing punctuation, or garbled "
    "fragments. Fix ONLY obvious transcription errors. Do not translate, "
    "summarize, add commentary, or change the meaning. Keep the original "
    "language. Reply with the corrected transcript ONLY.\n\n"
    "Transcript:\n{text}"
)


async def _correct_local_transcript(
    *, project: ProjectRow, text: str, workflow: str | None,
) -> str:
    """Local ASR trades accuracy for a tiny CPU footprint — clean its output with
    one cheap chat:fast pass. Best-effort: any failure (no provider, budget cap,
    exception) falls back to the raw transcript — a proofreading step must never
    cost the caller a working answer."""
    if not text.strip():
        return text
    # Size the proofread budget to the transcript: a fixed cap TRUNCATED long voice
    # notes and, being non-empty, the cut-off text was returned as "corrected".
    budget = min(
        _LOCAL_ASR_CORRECTION_TOKEN_CAP,
        max(_LOCAL_ASR_CORRECTION_MAX_TOKENS,
            int(estimate_prompt_tokens([{"role": "user", "content": text}]) * 1.4)),
    )
    tag = f"{workflow}+asr-correct" if workflow else "asr-correct"
    try:
        outcome = await run_chat(
            project=project, capability="chat:fast",
            messages=[{"role": "user",
                       "content": _LOCAL_ASR_CORRECTION_PROMPT.format(text=text)}],
            model=None, max_tokens=budget,
            temperature=0.0, response_format=None, workflow=tag,
        )
    except Exception as e:  # noqa: BLE001 — proofreading must never sink a working transcript
        log.warning("local ASR correction pass failed: %s — returning raw transcript", e)
        return text
    if not isinstance(outcome, ChatOutcome):
        return text
    corrected = outcome.text.strip()
    # If the proofread came back far shorter than the raw (truncated / over-trimmed),
    # a COMPLETE raw transcript beats a cut-off "corrected" one.
    if corrected and len(corrected) < _LOCAL_ASR_CORRECTION_MIN_KEEP_RATIO * len(text):
        log.warning("local ASR correction returned %d chars vs %d raw — likely "
                    "truncated; keeping raw transcript", len(corrected), len(text))
        return text
    return corrected or text


def _transcribe_gate(provider: str):
    spec = spec_or_default(provider)

    def check(text: str, meta: dict[str, Any]) -> Rejection | None:
        # A provider whose empty output is untrusted (local's small model + VAD can
        # clip a REAL message to "") must not return a successful "" — that would
        # silently DROP the voice. Book it and escalate; a cloud provider's empty
        # is genuinely-silent audio (kept).
        if spec.empty_is_failure and not text.strip():
            return Rejection("EmptyBody", Flow.NEXT_PROVIDER, http_status=502, bill=False,
                             log_message=f"{provider} ASR returned empty transcript — "
                                         "escalating to the next transcription provider")
        return None

    return check


def _transcribe_call(model: str, audio: bytes, filename: str):
    async def call(plain: str):
        return await transcribe(model=model, audio=audio, filename=filename, api_key=plain)
    return call


async def run_transcribe(
    *,
    project: ProjectRow,
    audio: bytes,
    filename: str,
    workflow: str | None,
) -> TranscribeOutcome | None:
    """Audio → text, walking the 'transcription' chain, rotating keys within each
    provider. None → no key anywhere (503); TranscribeFailed → every provider
    errored (502)."""
    scope = scope_for("transcription")
    last_error: str | None = None
    any_key_seen = False

    for provider in chain_for("transcription"):
        # Rotate KEYS within the provider before moving on — one transient 429/
        # timeout must not skip the provider while healthy sibling keys sit idle.
        for _ in range(_max_keys(provider)):
            use_model = model_for(provider, "transcription")
            key = await pick_and_reserve(provider, scope=scope, project_id=project.id,
                                          models=[use_model] if use_model else None)
            if key is None:
                break  # no (more) available key for this provider → next provider
            any_key_seen = True
            if not use_model:
                break
            res = await run_attempt(Attempt(
                key=key, project=project, provider=provider, model=use_model,
                capability="transcription", workflow=workflow,
                estimated_cost=(0.0 if key.tier == "free"
                                else estimate_transcription_cost(use_model, len(audio))),
                call=_transcribe_call(use_model, audio, filename),
                check=_transcribe_gate(provider),
                cap_flow=Flow.NEXT_KEY,
            ))
            if res.flow is Flow.SUCCESS:
                text = res.payload
                if spec_or_default(provider).refine_transcript:
                    text = await _correct_local_transcript(
                        project=project, text=text, workflow=workflow)
                meta = res.meta
                return TranscribeOutcome(
                    text=text, provider=provider, model=use_model,
                    model_served=meta.get("model_served"),
                    cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
                    key_label=key.label, request_id=res.usage_id or 0,
                )
            if res.error is not None:
                last_error = _failure_text(res)
            if res.flow is Flow.BUDGET_EXHAUSTED:
                raise TranscribeFailed(_CAP_MESSAGE)
            if res.flow is Flow.NEXT_PROVIDER:
                break  # deterministic for this audio/model → next provider

    if not any_key_seen:
        return None
    raise TranscribeFailed(last_error or "all providers failed")
