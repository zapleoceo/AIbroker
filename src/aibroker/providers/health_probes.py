"""Cheapest possible call per provider — used by monitor to check key liveness."""
from __future__ import annotations

import asyncio
import logging
import re

import httpx

from aibroker.providers.provider_errors import is_billing_error
from aibroker.providers.registry import spec_or_default

log = logging.getLogger(__name__)


PROBE_TIMEOUT_S = 15


async def probe(
    provider: str, plain_key: str, account_id: str | None = None,
    billable: bool = False,
) -> tuple[str, int, str]:
    """Returns (verdict, http_status, hint).
    verdict in {alive, cooldown, dead, neterr, skip}."""
    verdict, code, hint, _ = await probe_with_headers(
        provider, plain_key, account_id, billable)
    return verdict, code, hint


async def probe_with_headers(
    provider: str, plain_key: str, account_id: str | None = None,
    billable: bool = False,
) -> tuple[str, int, str, dict[str, str]]:
    """Same as probe() but also returns the provider's response headers — used
    by the key-create flow to extract published rate limits via
    extract_quota_headers(). Empty dict on network error.

    An UNPROBEABLE key (no probe configured for the provider, or a cloudflare
    key missing its account_id) returns the neutral verdict "skip", NOT
    "alive": the old force-"alive" default made the monitor RESURRECT a
    dead/revoked key of any unprobed provider on every sweep (is_alive=True,
    last_error wiped), so it flapped pick→fail→dead→revive forever
    (cloudflare, caught 2026-07-16). "skip" tells the monitor to leave the
    key's state exactly as real traffic left it.

    `billable` (paid or billing-dead key): use the provider's 1-token
    generation `billing_probe` when its normal probe is a free list endpoint —
    a list 200s for a key with depleted credits, which made the monitor revive
    a billing-dead gemini key every sweep (2026-10-04)."""
    spec = spec_or_default(provider)
    cfg = (spec.billing_probe if billable and spec.billing_probe else spec.probe)
    if cfg is None:
        return "skip", 0, "no probe configured", {}

    req = cfg.build(plain_key, account_id)
    if req is None:
        return "skip", 0, "unprobeable key (missing account_id)", {}
    method, url, headers, body = req
    try:
        async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as c:
            r = await c.request(method, url, headers=headers, json=body)
    except Exception as e:
        return "neterr", 0, f"{type(e).__name__}: {e}", {}

    h = dict(r.headers)
    b = r.text.lower()
    if 200 <= r.status_code < 300:
        return "alive", r.status_code, "", h
    # Out of money can arrive as 400/402/429 — never a throttle or a live key.
    if is_billing_error(b):
        return "dead", r.status_code, "no funds", h
    if r.status_code == 429:
        return "cooldown", 429, "rate limit", h
    if r.status_code in (401, 403):
        if "insufficient" in b or "balance" in b or "payment" in b:
            return "dead", r.status_code, "no funds", h
        # mistral's bare 401 "Unauthorized" on our accounts = monthly Vibe-plan
        # quota exhausted, not a revoked key (see llm_service / cooldown). Treat
        # it as a cooldown (key stays alive, cooled to the billing-cycle reset
        # by the monitor's monthly branch), so the probe doesn't re-kill a key
        # the request path correctly cooled.
        if provider == "mistral":
            return "cooldown", r.status_code, "monthly quota", h
        return "dead", r.status_code, "auth failed", h
    if r.status_code == 402:
        return "dead", 402, "payment required", h
    # Google answers a bad key with HTTP 400 (API_KEY_INVALID), not 401/403 —
    # without this a revoked gemini key read "alive/uncertain" forever.
    if r.status_code == 400 and ("api key not valid" in b or "api_key_invalid" in b):
        return "dead", 400, "auth failed", h
    return "alive", r.status_code, "uncertain", h


# Headers different providers use to advertise their rate limits. Mostly
# OpenAI-compat (x-ratelimit-limit-{requests,tokens}); Anthropic has its own
# prefix; Gemini/Cohere/Voyage don't expose useful daily limits in headers.
_DAY_VARIANTS = (
    "-day", "-1d", "-daily", "",   # ascending specificity
)


def _read_int(headers: dict[str, str], *keys: str) -> int | None:
    """Pull the first key present that parses to a positive int."""
    h = {k.lower(): v for k, v in headers.items()}
    for k in keys:
        v = h.get(k.lower())
        if not v:
            continue
        try:
            n = int(str(v).strip())
            if n > 0:
                return n
        except (TypeError, ValueError):
            continue
    return None


# "1h33m36s", "547ms", "2400s", "1d" — the provider's own reset-window duration
# strings (groq/OpenAI-compat style). Used to sanity-check whether a bare
# (non -day-suffixed) rate-limit header is actually daily-scoped.
_DURATION_RE = re.compile(
    r"(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m(?!s))?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)s)?(?:(?P<millis>\d+)ms)?"
)


def _parse_duration_seconds(value: str) -> float | None:
    m = _DURATION_RE.fullmatch(value.strip()) if value else None
    if not m or not any(m.groups()):
        return None
    return (
        int(m.group("days") or 0) * 86400
        + int(m.group("hours") or 0) * 3600
        + int(m.group("minutes") or 0) * 60
        + float(m.group("seconds") or 0)
        + int(m.group("millis") or 0) / 1000
    )


# A bare limit header is trusted as a DAILY cap only if its own reset window is
# within this margin of 24h. Groq's bare x-ratelimit-limit-tokens resets in
# ~500ms (a rolling TPM bucket) and x-ratelimit-limit-requests in ~1h33m (not a
# day either) — a single key logged 90k-170k tokens/day against an "8000
# tokens/day" reading from this header, instantly red on the dashboard despite
# being perfectly healthy. Requiring a near-24h reset (the provider's OWN
# signal, not a guess) rejects sub-day buckets instead of mis-storing them.
_MIN_DAILY_RESET_S = 20 * 3600


def _read_daily_int(headers: dict[str, str], limit_key: str, reset_key: str) -> int | None:
    """Like `_read_int`, but for a header with NO -day/-1d variant: only trust
    it as daily if `reset_key`'s duration is close to 24h."""
    h = {k.lower(): v for k, v in headers.items()}
    reset_s = _parse_duration_seconds(h.get(reset_key.lower(), ""))
    if reset_s is None or reset_s < _MIN_DAILY_RESET_S:
        return None
    return _read_int(headers, limit_key)


def extract_quota_headers(
    provider: str, headers: dict[str, str]
) -> tuple[int | None, int | None]:
    """Best-effort parse of (requests/day, tokens/day) from provider headers.

    Returns (None, None) when the provider doesn't expose these. Per-provider
    header names cribbed from each provider's docs as of 2026-06-28.
    """
    if spec_or_default(provider).quota_headers == "anthropic":
        return (
            _read_int(headers, "anthropic-ratelimit-requests-limit"),
            _read_int(headers, "anthropic-ratelimit-tokens-limit"),
        )
    # OpenAI-compat family (groq, openai, deepseek, mistral, openrouter, cerebras,
    # sambanova — confirmed same x-ratelimit-limit-requests-day header live 2026-07-04)
    spec = spec_or_default(provider)
    if spec.quota_headers == "openai":
        req = _read_int(headers, "x-ratelimit-limit-requests-day",
                          "x-ratelimit-limit-requests-1d")
        if req is None:
            req = _read_daily_int(headers, "x-ratelimit-limit-requests",
                                    "x-ratelimit-reset-requests")
        tok = _read_int(headers, "x-ratelimit-limit-tokens-day",
                          "x-ratelimit-limit-tokens-1d")
        if tok is None:
            tok = _read_daily_int(headers, "x-ratelimit-limit-tokens",
                                    "x-ratelimit-reset-tokens")
        # cerebras' requests-day header (2400 for gpt-oss-120b) isn't a hard
        # cap — a single key logged 4,866 req without a 429. It meters on
        # tokens, so drop the req axis to avoid a false >100% on the dashboard.
        if not spec.trust_req_header:
            req = None
        return req, tok
    # gemini / cohere / voyage — no documented daily-limit headers
    return None, None


# Probe request shapes live on ProviderSpec.probe (providers/specs.py).


async def probe_all(
    keys: list[tuple],
) -> dict[int, tuple[str, int, str]]:
    """keys: list of (api_key_id, provider, plain_token, account_id[, billable])."""
    sem = asyncio.Semaphore(8)

    async def one(kid: int, provider: str, plain: str, account_id: str | None,
                  billable: bool = False):
        async with sem:
            return kid, await probe(provider, plain, account_id, billable)

    out: dict[int, tuple[str, int, str]] = {}
    tasks = [one(*entry) for entry in keys]
    for coro in asyncio.as_completed(tasks):
        kid, result = await coro
        out[kid] = result
    return out
