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
# the exception: nemotron legitimately runs minutes (it's an async job, polled
# by deep_jobs with a 20-min stale marker), so it gets a long ceiling that
# still fires before the job is marked stale.
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
)

# 2026-07-05: confirmed live — Anthropic's "default" key had been failing
# ~2743 times/day with this exact message, classified as generic 'error' (no
# mark_dead), so it kept getting picked and kept failing at zero cost to the
# key itself but real waste on every request that reached anthropic in its
# chain. This is a billing/credentials problem, not a transient one — same
# bucket as 401/403: mark_dead stops real traffic from hitting it, and the
# monitor's own probe (independent of is_alive) keeps checking every
# MONITOR_INTERVAL_S and auto-revives it the moment credits are topped up.
# "credit balance is too low" is generic-billing enough to match any provider.
_AUTH_SIGNS = (
    "credit balance is too low",
)

# Billing exhaustion that arrives as an HTTP 429 (not 401/403), so it would
# otherwise match the generic rate-limit signs and churn on a short cooldown
# forever instead of being marked dead. Gemini returns 429 "Your prepayment
# credits are depleted" for a PAID key that ran out of money (confirmed live
# 2026-07-10). Treat as auth → mark_dead; the monitor's probe auto-revives the
# key the moment the balance is topped up. Checked BEFORE the rate-limit signs.
_BILLING_DEPLETED_SIGNS = (
    "prepayment credits are depleted",
    "credits are depleted",
    "insufficient balance",
)

# Provider-SCOPED signatures: applied ONLY when the failing key belongs to that
# provider. These are narrow, provider-specific error strings we caught live;
# putting them in the global lists risked mis-penalising an unrelated
# provider's healthy key on a superficially-similar message (e.g. a request we
# built wrong eliciting "invalid api parameter" would have mark_dead'd that
# provider's key). Scoping keeps each fix surgical.
_PROVIDER_RATE_LIMIT_SIGNS: dict[str, tuple[str, ...]] = {
    # DeepSeek "This response_format type is unavailable now" — confirmed live
    # (2026-07-05), hit every deepseek key identically (a provider-side feature
    # outage, not one bad key), ~2510 wasted attempts/day. Not literally a rate
    # limit, but the wanted behaviour (throttle, don't mark_dead — the
    # credential is fine) is rate_limit's.
    "deepseek": ("response_format type is unavailable",),
    # Voyage "no payment method on file … reduced rate limits of 3 RPM and 10K
    # TPM" — confirmed live (2026-07-07). The account is throttled to a lower
    # ceiling, not dead/unauthorized: cooldown, don't mark_dead.
    "voyage": ("reduced rate limits",),
    # Mistral bare 401 "Unauthorized" — on OUR 7 accounts this is the monthly
    # Vibe-plan call allowance being exhausted, NOT a revoked key (confirmed
    # 2026-07 via Mistral's admin console; the API text is indistinguishable
    # from a real revocation, so we treat every mistral 401 as monthly). Was
    # classified `auth` → mark_dead (dashboard: "мёртв/auth failed") when the
    # key is actually fine and returns on the billing-cycle reset. As a
    # rate_limit it cools instead; `cooldown.cooldown_until` resolves it to
    # next-month (see the provider-monthly rule there), and the key stays
    # is_alive — the honest state: "monthly quota, resets DATE", not dead.
    "mistral": ("unauthorized",),
}
_PROVIDER_AUTH_SIGNS: dict[str, tuple[str, ...]] = {
    # zai "Invalid API parameter, please check the documentation" — confirmed
    # live during the 2026-07-07 incident: key "eatmeat" hit it on 3141 of
    # ~3189 attempts (98.5%) while every other zai key succeeded normally. A
    # persistent per-account config problem, not transient: mark_dead stops
    # real traffic; the monitor's probe auto-revives it once fixed. Scoped to
    # zai so a request-construction bug on another provider can't kill its key.
    "zai": ("invalid api parameter",),
}


def classify_provider_error(exc: Exception, provider: str | None = None) -> str:
    """Map a provider exception to one of: 'rate_limit', 'auth', 'error'.

    Single source of truth — both chat and embed paths classify the same way.
    `provider` enables provider-scoped signatures (narrow strings that must not
    penalise other providers' keys); omit it to match only the global signs.
    """
    # 2026-07-07: our own call-timeout backstop (litellm_adapter.call_llm's
    # asyncio.wait_for) raises a bare TimeoutError with NO message — none of
    # the string-substring signs below can ever match it. Confirmed live: a
    # slow/overloaded zai key was taking 90-180s per call (real completions,
    # not hangs) well past our timeout ceiling; without this, the timeout
    # would classify as generic 'error' (no cooldown) and the same overloaded
    # key gets hit again immediately with zero backoff — the exact failure
    # mode this whole classifier exists to prevent. A provider/key that's
    # currently too slow is transient overload, not a dead credential.
    if isinstance(exc, TimeoutError):
        return "rate_limit"
    emsg = str(exc).lower()
    # Billing exhaustion first — a "credits depleted" 429 is an out-of-money
    # (auth) state, NOT a throttle; it must not fall through to rate_limit below.
    if any(s in emsg for s in _BILLING_DEPLETED_SIGNS):
        return "auth"
    if any(sign in emsg for sign in _RATE_LIMIT_SIGNS):
        return "rate_limit"
    if provider and any(s in emsg for s in _PROVIDER_RATE_LIMIT_SIGNS.get(provider, ())):
        return "rate_limit"
    if any(sign in emsg for sign in _AUTH_SIGNS) or "401" in emsg or "403" in emsg or "auth" in emsg:
        return "auth"
    if provider and any(s in emsg for s in _PROVIDER_AUTH_SIGNS.get(provider, ())):
        return "auth"
    return "error"


# Signatures that mean "this specific MODEL is gone/unprovisioned" (not the
# key, not a rate limit). The key itself is fine — its OTHER models still work
# — so we must NOT cooldown/mark_dead the key; we break to the next provider
# (sibling keys of this provider run the same dead model). Interim, model-level
# fix for the drift problem (nvidia kimi-k2.6 → 404 "Function not found for
# account", ~30 err/hr) until the per-(provider,model) handler lands (roadmap
# §3.1). litellm raises NotFoundError; the body carries these phrasings.
_MODEL_UNAVAILABLE_SIGNS = (
    "not found for account",
    "model_not_found",
    "does not exist",
    "no such model",
)


def _is_model_unavailable(exc: Exception) -> bool:
    if type(exc).__name__ == "NotFoundError":
        return True
    emsg = str(exc).lower()
    return any(sign in emsg for sign in _MODEL_UNAVAILABLE_SIGNS)


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
        try:
            from aibroker.routing.cooldown import cooldown_until
            until = await cooldown_until(key.id, key.provider, str(exc))
        except Exception:
            until = datetime.now(UTC) + _COOLDOWN
        await mark_cooldown(key.id, until, reason)
    elif kind == "auth":
        await mark_dead(key.id, reason)
    return kind


def _is_timeout(exc: Exception) -> bool:
    """True if the attempt died on OUR call-timeout backstop or the provider's
    own timeout. Distinct from a pre-processing reject (429/auth/503): on a
    timeout the provider HELD the request long enough to generate — and BILL —
    a response we never received (verified 2026-07-12: Google billed $122 on the
    paid gemini key while the broker recorded $2, the gap being ~1.2k/day gemini
    timeouts booked at $0). So a timeout must charge the cap, not be free."""
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


async def _record_error(
    *, key: ApiKeyRow, project: ProjectRow, provider: str, model: str,
    capability: str, workflow: str | None, exc: Exception,
    billed_cost: float = 0.0,
) -> None:
    """Book a failed attempt in usage_log. Shared by run_chat/run_embed/
    run_transcribe — the shape is identical; only the capability differs.

    `billed_cost` is normally 0 (a rejected call costs nothing), but a TIMEOUT
    the provider still processed IS billed upstream — the caller passes the
    reserved estimate so the per-key daily_cost_cap actually sees that spend and
    stops the key, instead of the cap staying blind to it (fix 2026-07-12).

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
        workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=billed_cost,
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

    # Dynamic per-request attempt budget = "try every key we have across the
    # whole chain before giving up", so the paid tail is always reached before
    # a 503 (a saturated provider yields no key → 0 attempts, so the chain
    # falls through to it fast). Bounded by the absolute runaway backstop.
    attempt_cap = _attempt_budget(chain)
    call_timeout = _call_timeout(capability)
    attempts = 0
    for provider in chain:
        empty_retries = 0  # bounded per provider — see the empty-body branch below
        for _ in range(_max_keys(provider)):
            if attempts >= attempt_cap:
                log.warning("chat:%s hit per-request attempt cap (%d) — 503",
                            capability, attempt_cap)
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
                    timeout=call_timeout,
                )
                meta["cost_usd"] = _billed_cost(key, meta)
            except Exception as e:  # noqa: BLE001 — classify, cool the key, try next
                # Attempt is over (however it ends) — release the reservation.
                # record_usage below books the real cost (0 here — no response).
                await release_cost(api_key=key, estimated_cost=estimated_cost)
                # Model gone/unprovisioned (404) — it's a MODEL problem, not a
                # KEY one: this key's OTHER models still work, and sibling keys
                # of this provider run the same dead model. Do NOT penalize the
                # key; break straight to the next provider. (Interim model-level
                # fix — see _is_model_unavailable / roadmap §3.1.)
                if _is_model_unavailable(e):
                    await _record_error(
                        key=key, project=project, provider=provider,
                        model=use_model, capability=capability, workflow=workflow, exc=e,
                    )
                    log.warning("provider %s model %s unavailable (%s) — next provider",
                                provider, use_model, type(e).__name__)
                    break  # next provider, not next key of the same dead model
                kind = await _penalize(key, e)
                # Self-learn the size ceiling: if the provider rejected the
                # prompt for being too big, remember it so we skip this
                # provider for prompts ≥ this size next time (no hardcoded cap).
                if is_too_large_error(e):
                    await record_too_large(provider, est_tokens)
                    log.info("learned: %s rejects ~%d tok prompts",
                             provider, est_tokens)
                    break  # bigger keys won't help — go straight to next provider
                # A timeout means the provider held the request long enough to
                # generate (and bill) a response we never got — charge the
                # reserved estimate so the daily_cost_cap sees that spend. A
                # pre-processing reject (429/auth/503) cost nothing → 0.
                await _record_error(
                    key=key, project=project, provider=provider,
                    model=use_model, capability=capability, workflow=workflow, exc=e,
                    billed_cost=estimated_cost if _is_timeout(e) else 0.0,
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
                if empty and empty_retries < _MAX_EMPTY_RETRIES:
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
            await _record_error(
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
            await _record_error(
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
        return TranscribeOutcome(
            text=text, provider=provider, model=use_model,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            key_label=key.label, request_id=request_id,
        )

    if not any_key_seen:
        return None
    raise TranscribeFailed(str(last_exc) if last_exc else "all providers failed")
