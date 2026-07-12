"""Provider-error classification — one home for the sign tables and verdicts.

Single source of truth: chat, embed, transcribe and the monitor probe must all
classify a provider exception the same way. The sign tables below are calibrated
against LIVE incidents (dates in the comments) and version-specific litellm
behaviour — treat every entry as load-bearing documentation.
"""
from __future__ import annotations

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


def is_model_unavailable(exc: Exception) -> bool:
    if type(exc).__name__ == "NotFoundError":
        return True
    emsg = str(exc).lower()
    return any(sign in emsg for sign in _MODEL_UNAVAILABLE_SIGNS)


def is_timeout(exc: Exception) -> bool:
    """True if the attempt died on OUR call-timeout backstop or the provider's
    own timeout. Distinct from a pre-processing reject (429/auth/503): on a
    timeout the provider HELD the request long enough to generate — and BILL —
    a response we never received (verified 2026-07-12: Google billed $122 on the
    paid gemini key while the broker recorded $2, the gap being ~1.2k/day gemini
    timeouts booked at $0). So a timeout must charge the cap, not be free."""
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()
