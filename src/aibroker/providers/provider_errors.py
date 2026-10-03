"""Provider-error classification — one home for the sign tables and verdicts.

Single source of truth: chat, embed and transcribe must all classify a
provider exception the same way (the monitor probe classifies via HTTP status
separately — see providers/health_probes.py). The sign tables below are
calibrated against LIVE incidents (dates in the comments) and
version-specific litellm behaviour — treat every entry as load-bearing
documentation. This includes the quota-DURATION marker tables at the bottom,
consumed by routing/cooldown.py.
"""
from __future__ import annotations

import re

from aibroker.providers.registry import spec_or_default

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
#
# 2026-10-03 review: bare "429"/"401"/"403"/"auth"/"quota" SUBSTRINGS anywhere
# in a message misfired (a request id "...4291...", "author", a 400 about a
# "quota" parameter) and killed healthy keys. Classification now goes
# status/exception-type first, then these phrase tables, with the former bare
# tokens as anchored word-boundary regexes (_QUOTA_WORD_RE / _AUTH_WORD_RE) —
# see classify_provider_error.
_RATE_LIMIT_SIGNS = (
    "rate_limit",
    "ratelimit",
    "resource_exhausted",
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

# Provider-SCOPED signatures (narrow strings that must not penalise another
# provider's key) live on ProviderSpec: rate_limit_signs / auth_signs /
# monthly_signs - see providers/specs.py; history in docs/history/provider-choices.md.


# HTTP status as it appears in provider/litellm message bodies: "Error code:
# 401", '"code": 429', "status_code=403", a leading "429 Too Many Requests",
# and the raw gemini ASR adapter's RuntimeError("gemini-asr 429: ..."). Anchored
# on a keyword so an id like "req_4291x" or "line 429" cannot be mistaken for a
# status.
_STATUS_RE = re.compile(
    r"(?:\b(?:status(?:[ _]code)?|code|http|error|gemini-asr)\b\W{0,4}|^\s*)(\d{3})\b",
    re.IGNORECASE,
)
# 'quota' / 'auth' as WHOLE WORDS only (not "author", "oauth", "quotation").
_QUOTA_WORD_RE = re.compile(r"\bquotas?\b")
_AUTH_WORD_RE = re.compile(
    r"\b(?:auth|authentication|authenticationerror|unauthori[sz]ed|unauthenticated"
    r"|forbidden|permissiondeniederror|permission_denied)\b"
)
# Bad-credential phrasings that arrive as HTTP 400 (gemini: 400 "API key not
# valid") — unconditional, they never describe anything but a bad key.
_BAD_KEY_RE = re.compile(
    r"\b(?:api key not valid|invalid[ _-]?api[ _-]?key|incorrect api key)\b"
)
_AUTH_EXC_NAMES = frozenset({"AuthenticationError", "PermissionDeniedError"})
_RATE_EXC_NAMES = frozenset({"RateLimitError"})
# Client errors that say nothing about key health (bad request, 404, 413, 422).
_NEUTRAL_4XX_EXCLUDED = frozenset({401, 403, 408, 429})


def _exc_names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


def _attr_status(exc: BaseException) -> int | None:
    """HTTP status carried by the exception object (litellm sets status_code;
    httpx errors carry .response)."""
    for src in (exc, getattr(exc, "response", None)):
        code = getattr(src, "status_code", None)
        if isinstance(code, int) and 100 <= code <= 599:
            return code
    return None


def _message_statuses(emsg: str) -> set[int]:
    return {int(m) for m in _STATUS_RE.findall(emsg)}


def http_status_of(exc: BaseException) -> int | None:
    """The HTTP status the provider ACTUALLY returned, when the exception tells
    us (status_code attribute, else an anchored status in the message), else
    None. What usage_log.http_status stores - never a status inferred from our
    own classification (see llm_service._record_error)."""
    attr = _attr_status(exc)
    if attr is not None:
        return attr
    found = _STATUS_RE.findall(str(exc).lower())
    return next((int(c) for c in found if 100 <= int(c) <= 599), None)


def classify_provider_error(exc: Exception, provider: str | None = None) -> str:
    """Map a provider exception to one of: 'rate_limit', 'auth', 'error'.

    Single source of truth — both chat and embed paths classify the same way.
    `provider` enables provider-scoped signatures (narrow strings that must not
    penalise other providers' keys); omit it to match only the global signs.

    Order (2026-10-03): billing-depleted phrases → provider-scoped rate-limit
    phrases (mistral's 401 is a monthly quota) → exception TYPE / HTTP status
    (RateLimitError / 429 → rate_limit; AuthenticationError / 401 / 403 → auth)
    → global phrase tables, skipped when the exception carries an explicit
    neutral 4xx status (a 400 that merely mentions "quota" is not a throttle)
    → provider-scoped auth phrases. litellm sometimes MIS-types a provider's
    429 (cohere → APIConnectionError, status 500), which is why the phrase
    tables still apply to untyped / 5xx errors.
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
    if provider and any(s in emsg for s in spec_or_default(provider).rate_limit_signs):
        return "rate_limit"
    names = _exc_names(exc)
    attr = _attr_status(exc)
    statuses = _message_statuses(emsg) | ({attr} if attr else set())
    if 429 in statuses or names & _RATE_EXC_NAMES:
        return "rate_limit"
    if (any(sign in emsg for sign in _AUTH_SIGNS) or _BAD_KEY_RE.search(emsg)
            or statuses & {401, 403} or names & _AUTH_EXC_NAMES):
        return "auth"
    neutral_4xx = attr is not None and 400 <= attr < 500 and attr not in _NEUTRAL_4XX_EXCLUDED
    if not neutral_4xx:
        if any(sign in emsg for sign in _RATE_LIMIT_SIGNS) or _QUOTA_WORD_RE.search(emsg):
            return "rate_limit"
        if _AUTH_WORD_RE.search(emsg):
            return "auth"
    if provider and any(s in emsg for s in spec_or_default(provider).auth_signs):
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
    timeout the provider HELD the request for a long time. Used to steepen the
    cooldown for a hanging key (a ~60s-wasted timeout escalates faster than a
    0s-wasted 429) and to feed the timeout circuit-breaker.

    NOT a billing signal: since 2026-07-16 an answerless timeout books $0 and
    its reservation is fully released (see llm_service._record_error) — the
    upstream spend of a timed-out call is reconciled against the provider
    invoice out-of-band, not charged to the admission cap. (This docstring used
    to say a timeout "must charge the cap"; that was reversed on 2026-07-16.)"""
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


# ─── Quota-DURATION markers (how long to park a rate-limited key) ────────────
# Consumed by routing/cooldown.py's is_*_quota_error / _is_provider_monthly.
# They live HERE (moved from cooldown.py, 2026-07-16) so every
# provider-message sign table has one home and the two files can't drift.

# A key that hit its DAILY quota won't recover until the provider's day rolls
# over (UTC midnight for the ones we use). Parking it 60 s just causes a retry
# storm — it 429s again immediately. Markers below mean "daily exhaustion".
_DAILY_QUOTA_MARKERS = (
    "per day",
    "per-day",
    # Gemini quotaId "GenerateRequestsPerDayPerProjectPerModel-FreeTier" —
    # CamelCase, so "per day" never matches it.
    "perday",
    "tokens per day",
    "daily limit",
    "requests per day",
    "tpd",
    "rpd",
    # cloudflare Workers AI: "daily free allocation of 10,000 neurons" —
    # resets at 00:00 UTC like every other daily quota here (2026-07-12).
    "daily free allocation",
)

# A per-HOUR request cap (cerebras free: "Requests per hour limit exceeded").
# Distinct from per-minute (recovers in ~60s → adaptive) and per-day (waits to
# UTC midnight). Parking 60s just re-hits the wall and climbs the adaptive
# backoff one 429 at a time; park to the top of the next hour on the first hit.
_HOURLY_QUOTA_MARKERS = (
    "per hour",
    "per-hour",
    "requests per hour",
    "hourly limit",
)

# A per-MONTH call cap (cohere trial: "You are using a Trial key, which is
# limited to 1000 API calls / month"). This is NOT a rate-limit that clears in
# minutes/hours/a day — the account's monthly allowance is gone until the
# provider's billing cycle rolls over. Confirmed live (2026-07-03): all 7
# cohere keys are exhausted trial keys; the adaptive 60s-doubling backoff was
# the only thing applying (worse: classify_provider_error didn't even
# recognise "trial key"/"1000 API calls" as rate-limiting at all, so
# _penalize did NOTHING — no cooldown, no mark_dead — and the exhausted key
# was retried on every single pick with zero backoff, 1447 wasted attempts in
# 17h). Anything shorter than "next month" just re-hits the same wall.
_MONTHLY_QUOTA_MARKERS = (
    "trial key",
    "api calls / month",
    "calls / month",
    "monthly limit",
)
