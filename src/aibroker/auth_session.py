"""Telegram Login Widget verification + HMAC cookie sessions.

Flow:
  1. Browser loads /login → renders TG Login Widget pointed at /api/tg_login
  2. User clicks "Login with Telegram" → TG returns query params
  3. We verify HMAC-SHA256 signature using bot_token-derived key
  4. We check user_id == OWNER_TELEGRAM_ID (single-owner broker)
  5. We set an HMAC-signed cookie with TTL — browser is now authenticated
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request

from aibroker.config import get_settings

log = logging.getLogger(__name__)

COOKIE_NAME = "aib_session"
SESSION_TTL_S = 30 * 24 * 3600   # 30 days
TG_AUTH_FRESHNESS_S = 24 * 3600  # accept TG widget auth_date within 1 day


def verify_telegram_widget(data: dict[str, str]) -> int | None:
    """Verify TG Login Widget signature. Returns Telegram user_id or None."""
    s = get_settings()
    if not s.TELEGRAM_BOT_TOKEN:
        return None
    incoming_hash = data.get("hash")
    if not incoming_hash:
        return None
    pairs = sorted(
        (k, v) for k, v in data.items()
        if k != "hash" and v not in (None, "")
    )
    data_check_string = "\n".join(f"{k}={v}" for k, v in pairs)
    secret_key = hashlib.sha256(s.TELEGRAM_BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, incoming_hash):
        log.warning("TG widget signature mismatch")
        return None
    try:
        auth_date = int(data.get("auth_date", "0"))
    except ValueError:
        return None
    if abs(time.time() - auth_date) > TG_AUTH_FRESHNESS_S:
        log.warning("TG widget auth_date too old")
        return None
    try:
        return int(data["id"])
    except (KeyError, ValueError):
        return None


# ─── Session cookies (HMAC-SHA256 signed) ───────────────────────────────────


def _session_secret() -> str:
    s = get_settings().SESSION_SECRET
    if not s:
        raise RuntimeError("SESSION_SECRET not configured")
    return s


def issue_session_cookie(user_id: int) -> tuple[str, int]:
    """Return (cookie_value, max_age_seconds). Format: <user_id>.<exp>.<hmac>."""
    exp = int(time.time()) + SESSION_TTL_S
    payload = f"{user_id}.{exp}"
    sig = hmac.new(_session_secret().encode(), payload.encode(),
                    hashlib.sha256).hexdigest()
    return f"{payload}.{sig}", SESSION_TTL_S


def verify_session_cookie(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split(".")
    if len(parts) != 3:
        return None
    user_id_s, exp_s, sig = parts
    try:
        user_id = int(user_id_s)
        exp = int(exp_s)
    except ValueError:
        return None
    if exp < time.time():
        return None
    expected = hmac.new(_session_secret().encode(),
                        f"{user_id}.{exp}".encode(),
                        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return None
    return user_id


# ─── FastAPI dependency ─────────────────────────────────────────────────────


@dataclass
class OwnerSession:
    user_id: int


def require_owner_session(request: Request) -> OwnerSession:
    """Accept either valid session cookie OR X-Admin-Key (for curl/CI)."""
    s = get_settings()
    # 1. Admin key fallback — for curl/ops without browser
    admin_hdr = request.headers.get("x-admin-key")
    if admin_hdr and hmac.compare_digest(admin_hdr, s.ADMIN_KEY):
        return OwnerSession(user_id=0)  # 0 = admin-key actor
    # 2. Cookie session
    uid = verify_session_cookie(request.cookies.get(COOKIE_NAME))
    if uid is not None and uid == s.OWNER_TELEGRAM_ID:
        return OwnerSession(user_id=uid)
    raise HTTPException(status_code=401, detail="login required")
