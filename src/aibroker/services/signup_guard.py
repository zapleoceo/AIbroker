"""Anti-bot guards for the public self-signup (services/signup.py).

Proof of work: GET /v1/signup/challenge hands out a stateless, HMAC-signed challenge
`<issued_unix>.<random hex>.<signature>`. The client finds a decimal `nonce` such that
sha256(f"{challenge}:{nonce}") has at least SIGNUP_POW_BITS leading zero bits (18 bits:
about 260k hashes, a second or two in a browser, a fraction of that in Python) and sends
both with POST /v1/signup. A challenge is valid for SIGNUP_POW_TTL_S and only once
(Redis SET NX when REDIS_URL is set, else a per-process set).

Disposable email: a contact email on a known throwaway domain is refused.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any

from aibroker.config import get_settings

log = logging.getLogger(__name__)

_USED_LOCAL: dict[str, float] = {}

# Common throwaway-mail domains (refused as a signup contact). Not exhaustive: the
# per-domain daily limit and the Telegram verification bound the rest.
DISPOSABLE_DOMAINS = frozenset({
    "10minutemail.com", "20minutemail.com", "burnermail.io", "discard.email",
    "dispostable.com", "emailondeck.com", "fakeinbox.com", "getairmail.com", "getnada.com",
    "guerrillamail.com", "guerrillamail.net", "guerrillamail.org", "guerrillamailblock.com",
    "inboxkitten.com", "mail.tm", "maildrop.cc", "mailinator.com", "mailnesia.com",
    "mailpoof.com", "mintemail.com", "mohmal.com", "moakt.com", "mytemp.email", "nada.email",
    "sharklasers.com", "spam4.me", "spamgourmet.com", "temp-mail.io", "temp-mail.org",
    "tempail.com", "tempmail.com", "tempmail.dev", "tempmail.net", "tempmailo.com",
    "tempr.email", "throwawaymail.com", "trashmail.com", "trashmail.de", "yopmail.com",
    "yopmail.fr", "yopmail.net",
})


class PowInvalid(ValueError):
    """Missing, forged, expired, reused or too-weak proof of work."""


def _secret() -> bytes:
    return hashlib.sha256(b"signup-pow:" + get_settings().TOKEN_SECRET.encode()).digest()


def _sign(body: str) -> str:
    return hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]


def issue_challenge(now: float | None = None) -> dict[str, Any]:
    s = get_settings()
    body = f"{int(now if now is not None else time.time())}.{secrets.token_hex(12)}"
    return {"challenge": f"{body}.{_sign(body)}", "difficulty_bits": s.SIGNUP_POW_BITS,
            "expires_in_s": s.SIGNUP_POW_TTL_S, "algorithm":
            "sha256(challenge + ':' + nonce) must start with difficulty_bits zero bits; "
            "nonce is a decimal integer"}


def leading_zero_bits(digest: bytes) -> int:
    bits = 0
    for byte in digest:
        if byte == 0:
            bits += 8
            continue
        return bits + (8 - byte.bit_length())
    return bits


def solve(challenge: str, bits: int) -> str:
    """Reference solver (tests, docs): the first nonce that meets `bits`."""
    nonce = 0
    while leading_zero_bits(hashlib.sha256(f"{challenge}:{nonce}".encode()).digest()) < bits:
        nonce += 1
    return str(nonce)


async def _first_use(challenge: str, ttl_s: int) -> bool:
    key = "aib:signup:pow:" + hashlib.sha256(challenge.encode()).hexdigest()[:32]
    url = os.environ.get("REDIS_URL", "")
    if url:
        try:
            import redis.asyncio as redis_asyncio
            client = redis_asyncio.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
            try:
                return bool(await client.set(key, "1", nx=True, ex=ttl_s))
            finally:
                await client.aclose()
        except Exception as e:  # noqa: BLE001 - fall back to the per-process guard
            log.warning("signup pow replay guard: redis unavailable (%s)", e)
    now = time.time()
    for k, exp in list(_USED_LOCAL.items()):
        if exp < now:
            del _USED_LOCAL[k]
    if key in _USED_LOCAL:
        return False
    _USED_LOCAL[key] = now + ttl_s
    return True


async def check_pow(challenge: str | None, nonce: str | None) -> None:
    """Raise PowInvalid unless (challenge, nonce) is a fresh, unused, valid proof."""
    s = get_settings()
    if s.SIGNUP_POW_BITS <= 0:
        return
    if not challenge or not nonce:
        raise PowInvalid("proof of work required: GET /v1/signup/challenge, solve it, "
                         "send pow.challenge and pow.nonce")
    try:
        issued, rand, sig = challenge.split(".")
        issued_at = int(issued)
    except ValueError as e:
        raise PowInvalid("malformed challenge") from e
    if not hmac.compare_digest(sig, _sign(f"{issued}.{rand}")):
        raise PowInvalid("challenge was not issued by this broker")
    if not 0 <= time.time() - issued_at <= s.SIGNUP_POW_TTL_S:
        raise PowInvalid("challenge expired; fetch a new one")
    if not nonce.isdigit() or len(nonce) > 20:
        raise PowInvalid("nonce must be a decimal integer")
    digest = hashlib.sha256(f"{challenge}:{nonce}".encode()).digest()
    if leading_zero_bits(digest) < s.SIGNUP_POW_BITS:
        raise PowInvalid(f"nonce does not meet {s.SIGNUP_POW_BITS} leading zero bits")
    if not await _first_use(challenge, s.SIGNUP_POW_TTL_S):
        raise PowInvalid("challenge already used; fetch a new one")


def email_domain(contact: str) -> str | None:
    """The lower-case domain of an email-looking contact, else None (a handle)."""
    if "@" not in contact:
        return None
    domain = contact.rsplit("@", 1)[1].strip().lower().rstrip(".")
    return domain or None


def is_disposable(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in DISPOSABLE_DOMAINS)
