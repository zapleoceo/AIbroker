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
from typing import Any

from aibroker.crypto import decrypt
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
from aibroker.routing import (
    CostGuardError,
    chain_for,
    deprioritize_for_json,
    pick_and_reserve,
    release_cost,
    reserve_cost,
    scope_for,
)
from aibroker.routing.selector import mark_cooldown, mark_dead, record_usage
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

# Hard ceiling on provider-call attempts for a single request, across the whole
# chain — a saturation storm could otherwise walk ~30 attempts (9 providers ×
# per-provider key retries) of pure latency before giving up. Bounds the tail.
_MAX_ATTEMPTS_PER_REQUEST = 12


def _max_keys(provider: str) -> int:
    return _MAX_KEYS_BY_PROVIDER.get(provider, _MAX_KEYS_PER_PROVIDER)


# Substrings (lower-cased) that mean a provider throttled us — covers the
# many shapes: '429', 'rate_limit' (underscore), 'ratelimiterror' (CamelCase
# from litellm/cerebras), Google's 'resource_exhausted', and the quota
# phrasings ('quota', 'tokens per day', 'too many tokens'). Missing any of
# these meant cerebras 'RateLimitError - Tokens per day' fell through to
# 'error' and the key was never cooled → infinite retry storm.
#
# 'trial key' / 'api calls / month' (2026-07-03): a LiteLLM 1.89.3 bug maps
# cohere's 429 quota response to litellm.APIConnectionError instead of
# RateLimitError (confirmed live: a real quota-exhausted cohere key raises
# APIConnectionError with status_code=500, both wrong). Cohere's trial-quota
# body says "You are using a Trial key, which is limited to 1000 API calls /
# month" — 'rate limits' (with a space) elsewhere in that message doesn't
# match 'ratelimit'/'rate_limit' above, so classify_provider_error fell
# through to generic 'error'. _penalize does NOTHING for 'error' (no
# cooldown, no mark_dead) — an exhausted key was retried on every single pick
# with zero backoff: 1447 wasted attempts / 17h before this fix.
_RATE_LIMIT_SIGNS = (
    "rate_limit",
    "ratelimit",
    "429",
    "resource_exhausted",
    "quota",
    "tokens per day",
    "tokens per minute",
    "too many tokens",
    "too many requests",
    "trial key",
    "api calls / month",
    # 2026-07-05: DeepSeek "This response_format type is unavailable now" —
    # confirmed live, ~2510 wasted attempts/day across every deepseek key
    # (veranda/eatmeat/levaromat/demoniwwwe/zapleosoft/itstep — not one bad
    # key, a provider-side feature outage). Falls through to generic 'error'
    # (no cooldown) without this, so every triage call re-hits the same
    # guaranteed failure on the next key pick with zero backoff. Not
    # literally a rate limit, but the desired behavior (throttle, don't
    # mark_dead — the credential is fine) is identical.
    "response_format type is unavailable",
    # 2026-07-07: Voyage's "no payment method on file" response — confirmed
    # live (docker logs, 24h window): dozens of hits/day across every voyage
    # key (lev/verandapay/eatmeat/itstep/...), zero backoff since it fell
    # through to generic 'error'. Real behavior: the account is throttled to
    # "reduced rate limits of 3 RPM and 10K TPM", not dead or unauthorized —
    # same bucket as any other rate limit (cooldown, don't mark_dead; a fresh
    # key or a short wait clears it, same free 200M-token budget still
    # applies once under the reduced ceiling).
    "reduced rate limits",
)

# 2026-07-05: confirmed live — Anthropic's "default" key had been failing
# ~2743 times/day with this exact message, classified as generic 'error' (no
# mark_dead), so it kept getting picked and kept failing at zero cost to the
# key itself but real waste on every request that reached anthropic in its
# chain. This is a billing/credentials problem, not a transient one — same
# bucket as 401/403: mark_dead stops real traffic from hitting it, and the
# monitor's own probe (independent of is_alive) keeps checking every
# MONITOR_INTERVAL_S and auto-revives it the moment credits are topped up.
_AUTH_SIGNS = (
    "credit balance is too low",
    # 2026-07-07: confirmed live during a real incident (cerebras/groq daily
    # quota exhaustion overflowed traffic onto zai) — zai key "eatmeat" hit
    # this EXACT message on 3141 of ~3189 attempts in 30 min (98.5%, only 2
    # successes), while every other zai key on the same account type/model
    # succeeded normally. Isolated to one key/account, not a shared zai
    # outage — a persistent config problem (unclear which param), not
    # transient. Was generic 'error' (no cooldown, no mark_dead), so it got
    # hammered on every pick with zero backoff. mark_dead stops real traffic;
    # the monitor's own probe keeps checking and auto-revives it once
    # whatever's misconfigured on that account is fixed.
    "invalid api parameter",
)


def classify_provider_error(exc: Exception) -> str:
    """Map a provider exception to one of: 'rate_limit', 'auth', 'error'.

    Single source of truth — both chat and embed paths classify the same way.
    """
    emsg = str(exc).lower()
    if any(sign in emsg for sign in _RATE_LIMIT_SIGNS):
        return "rate_limit"
    if any(sign in emsg for sign in _AUTH_SIGNS) or "401" in emsg or "403" in emsg or "auth" in emsg:
        return "auth"
    return "error"


async def _penalize(key: ApiKeyRow, exc: Exception) -> str:
    """Cooldown on rate-limit, mark dead on auth error. Returns the error kind."""
    kind = classify_provider_error(exc)
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
        try:
            from aibroker.routing.cooldown import cooldown_until
            until = await cooldown_until(key.id, key.provider, str(exc))
        except Exception:
            until = datetime.now(UTC) + _COOLDOWN
        await mark_cooldown(key.id, until, reason)
    elif kind == "auth":
        await mark_dead(key.id, reason)
    return kind


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
    Free-tier keys must always bill $0, whatever the model's list price is.

    EXCEPT voyage (2026-07-07): confirmed live via Voyage's own dashboard
    (Usage → Free Token tab) that `voyage-3` has a ZERO free-token allocation
    on our accounts — 0 used, 0 remaining, unlike voyage-context-3/voyage-4
    which get 200M free. A "free"-tier label on a voyage key does NOT mean
    $0 real cost here: real invoices arrived ($0.51 seen live) while our own
    tracking showed $0.00 for every single call, because this function
    zeroed it out unconditionally. Voyage always bills LiteLLM's real
    estimated cost regardless of our tier label — the free-tier assumption
    this function makes is simply false for this provider.
    """
    if key.provider == "voyage":
        return meta["cost_usd"]
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
) -> ChatOutcome | None:
    """Walk the capability chain; return the first provider that succeeds, else None.

    Within a provider, try up to `_MAX_KEYS_PER_PROVIDER` keys (the selector hands
    out a fresh LRU key each time and `_penalize` cools failed ones) before falling
    through — so one rate-limited free key doesn't sink the whole request.
    """
    scope = scope_for(capability)

    # Exact-match cache for deterministic capabilities (translate): the same
    # phrases recur verbatim, so a cached answer is correct and skips the whole
    # LLM round-trip. No-op for chat/* (not deterministic).
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
    # Size-aware provider filter: drop providers whose single-request token
    # ceiling can't fit this prompt (e.g. groq getting a 24k Coach prompt — a
    # guaranteed 413). Ceilings never drop below MIN_LEARNABLE_CEILING, so a
    # smaller prompt fits EVERY provider — skip the learned_ceilings() DB
    # round-trip entirely on the high-volume small-prompt path (chat:fast,
    # translate). Above the floor, filter as before (fall back to the full
    # chain if every provider is size-skipped, so we never starve).
    if est_tokens >= MIN_LEARNABLE_CEILING:
        learned = await learned_ceilings()
        sized_chain = [
            p for p in full_chain if fits_context(p, est_tokens, learned.get(p))
        ]
        chain = sized_chain or full_chain
        if len(sized_chain) < len(full_chain):
            log.info("chat:%s prompt ~%d tok — skipping over-ceiling providers: %s",
                     capability, est_tokens,
                     [p for p in full_chain if p not in sized_chain])
    else:
        chain = full_chain

    # Hard cap on total provider-call attempts per request — a 9-provider chain
    # × per-provider key retries could otherwise reach ~30 attempts of pure
    # latency in a saturation storm. Bound the tail; the chain still reaches
    # most providers at least once.
    attempts = 0
    for provider in chain:
        for _ in range(_max_keys(provider)):
            if attempts >= _MAX_ATTEMPTS_PER_REQUEST:
                log.warning("chat:%s hit per-request attempt cap (%d) — 503",
                            capability, _MAX_ATTEMPTS_PER_REQUEST)
                return None
            key = await pick_and_reserve(provider, scope=scope)
            if key is None:
                break  # no (more) available key for this provider → next provider
            attempts += 1
            use_model = model or model_for(provider, capability)
            if not use_model:
                break  # provider can't serve this capability → next provider
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
                break  # project/global cap — more keys won't help → next provider
            plain = decrypt(key.token_encrypted)
            try:
                text, meta = await call_llm(
                    model=use_model, messages=messages, api_key=plain,
                    max_tokens=max_tokens, temperature=temperature,
                    response_format=response_format,
                    extra=extra_for_provider(provider, getattr(key, "account_id", None)),
                )
                meta["cost_usd"] = _billed_cost(key, meta)
            except Exception as e:  # noqa: BLE001 — classify, cool the key, try next
                # Attempt is over (however it ends) — release the reservation.
                # record_usage below books the real cost (0 here — no response).
                await release_cost(api_key=key, estimated_cost=estimated_cost)
                kind = await _penalize(key, e)
                # Self-learn the size ceiling: if the provider rejected the
                # prompt for being too big, remember it so we skip this
                # provider for prompts ≥ this size next time (no hardcoded cap).
                if is_too_large_error(e):
                    await record_too_large(provider, est_tokens)
                    log.info("learned: %s rejects ~%d tok prompts",
                             provider, est_tokens)
                    break  # bigger keys won't help — go straight to next provider
                await record_usage(
                    api_key_id=key.id, project_id=project.id, lease_id=None,
                    provider=provider, model=use_model, capability=capability,
                    workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
                    latency_ms=None, status="error", error_kind=type(e).__name__,
                    http_status=None,
                )
                log.warning("provider %s key %s failed (%s): %s",
                            provider, key.label, kind, e)
                continue  # try the next key of this provider

            # Call resolved (successfully) — release the reservation; record_usage
            # below books the REAL final cost (meta["cost_usd"]) on top, so the
            # key ends up debited by exactly the real cost, never the estimate.
            await release_cost(api_key=key, estimated_cost=estimated_cost)

            # Deterministic JSON quality gate: an unparseable JSON body (gemini
            # truncated, deepseek rogue) is billed but treated as a failure.
            if _wants_json(response_format) and not _is_valid_json(text):
                await record_usage(
                    api_key_id=key.id, project_id=project.id, lease_id=None,
                    provider=provider, model=use_model, capability=capability,
                    workflow=workflow, tokens_in=meta["tokens_in"],
                    tokens_out=meta["tokens_out"], cost_usd=meta["cost_usd"],
                    cache_read_tokens=meta.get("cache_read_tokens", 0),
                    cache_write_tokens=meta.get("cache_write_tokens", 0),
                    latency_ms=meta["latency_ms"], status="error",
                    error_kind="InvalidJSON", http_status=200,
                )
                # Malformed JSON is a MODEL property, not a key one: cerebras
                # gpt-oss mangles the same prompt on every key. Retrying sibling
                # keys of this provider just burns tokens N more times (~80% of
                # the InvalidJSON waste). Skip straight to the next provider.
                log.warning("provider %s returned unparseable JSON, next provider", provider)
                break  # next provider, not next key of the same model

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
            # Cache deterministic (translate) successes for verbatim repeats.
            response_cache.put(capability, messages, text, model=model,
                                max_tokens=max_tokens, temperature=temperature)
            return ChatOutcome(
                text=text, provider=provider, model=meta["model"],
                tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
                cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
                key_label=key.label, request_id=request_id,
                cache_read_tokens=meta.get("cache_read_tokens", 0),
                cache_write_tokens=meta.get("cache_write_tokens", 0),
            )
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
        key = await pick_and_reserve(provider, scope=scope_for("embedding"))
        if key is None:
            break  # no (more) available key for this provider
        any_key_seen = True
        plain = decrypt(key.token_encrypted)
        try:
            vectors, meta = await embed(model=use_model, texts=inputs, api_key=plain)
            meta["cost_usd"] = _billed_cost(key, meta)
        except Exception as e:  # noqa: BLE001 — classify, cool the key, try next
            last_exc = e
            await _penalize(key, e)
            await record_usage(
                api_key_id=key.id, project_id=project.id, lease_id=None,
                provider=provider, model=use_model, capability="embedding",
                workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
                latency_ms=None, status="error", error_kind=type(e).__name__,
                http_status=None,
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
        key = await pick_and_reserve(provider, scope=scope)
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
            await _penalize(key, e)
            await record_usage(
                api_key_id=key.id, project_id=project.id, lease_id=None,
                provider=provider, model=use_model, capability="transcription",
                workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
                latency_ms=None, status="error", error_kind=type(e).__name__,
                http_status=None,
            )
            continue
        request_id = await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=use_model, capability="transcription",
            workflow=workflow, tokens_in=0, tokens_out=0,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            status="ok", error_kind=None, http_status=200,
        )
        return TranscribeOutcome(
            text=text, provider=provider, model=use_model,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            key_label=key.label, request_id=request_id,
        )

    if not any_key_seen:
        return None
    raise TranscribeFailed(str(last_exc) if last_exc else "all providers failed")
