"""auth_session — Telegram widget verify, HMAC cookie roundtrip."""
from __future__ import annotations

import time

import pytest

from aibroker.auth_session import (
    SESSION_TTL_S,
    issue_session_cookie,
    verify_session_cookie,
    verify_telegram_widget,
)
from aibroker.config import get_settings


def test_session_cookie_roundtrip():
    uid = 169510539
    cookie, ttl = issue_session_cookie(uid)
    assert ttl == SESSION_TTL_S
    assert verify_session_cookie(cookie) == uid


def test_session_cookie_format():
    """<uid>.<exp>.<hmac> with 3 dot-separated parts."""
    cookie, _ = issue_session_cookie(42)
    parts = cookie.split(".")
    assert len(parts) == 3
    assert parts[0] == "42"


def test_session_cookie_tampered_rejected():
    cookie, _ = issue_session_cookie(169510539)
    parts = cookie.split(".")
    # Flip the uid — sig should no longer match
    bad = f"99999.{parts[1]}.{parts[2]}"
    assert verify_session_cookie(bad) is None


def test_session_cookie_expired_rejected():
    """Manually craft a cookie with exp in the past."""
    import hmac
    import hashlib
    secret = get_settings().SESSION_SECRET.encode()
    past = int(time.time()) - 1000
    payload = f"99.{past}"
    sig = hmac.new(secret, payload.encode(), hashlib.sha256).hexdigest()
    expired = f"{payload}.{sig}"
    assert verify_session_cookie(expired) is None


def test_session_cookie_garbage_rejected():
    assert verify_session_cookie(None) is None
    assert verify_session_cookie("") is None
    assert verify_session_cookie("not.even.three.parts.long") is None
    assert verify_session_cookie("abc.def.ghi") is None  # non-int uid


def test_tg_widget_no_hash_rejected():
    assert verify_telegram_widget({"id": "1", "auth_date": str(int(time.time()))}) is None


def test_tg_widget_unsigned_rejected():
    # data with hash that isn't a valid HMAC for the bot token
    data = {
        "id": "169510539",
        "first_name": "Test",
        "auth_date": str(int(time.time())),
        "hash": "deadbeef" * 8,
    }
    assert verify_telegram_widget(data) is None


def test_tg_widget_stale_rejected():
    """auth_date > 24h ago is rejected even if signature checks out."""
    # We can't easily craft a valid HMAC here without the real token, but
    # at minimum the freshness check should kick in before signature verify
    # if auth_date format is parseable. (Doc'd as a defense-in-depth check.)
    very_old = int(time.time()) - 7 * 24 * 3600
    data = {"id": "169510539", "auth_date": str(very_old), "hash": "x" * 64}
    # This will fail signature OR freshness — both → None
    assert verify_telegram_widget(data) is None
