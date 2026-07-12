"""auth_session — Telegram widget verify, HMAC cookie roundtrip."""
from __future__ import annotations

import time

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
    import hashlib
    import hmac
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


def _signed_widget_data(bot_token: str, *, auth_date: int, user_id: int = 42) -> dict:
    """Build widget data with a VALID Telegram HMAC — the accept path."""
    import hashlib as _h
    import hmac as _hm
    data = {"id": str(user_id), "first_name": "D", "auth_date": str(auth_date)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = _h.sha256(bot_token.encode()).digest()
    data["hash"] = _hm.new(secret, check.encode(), _h.sha256).hexdigest()
    return data


def test_widget_accepts_valid_signature(monkeypatch):
    """REGRESSION GUARD: the owner-login ACCEPT path was never tested — only
    the early rejects. A valid HMAC within the freshness window must yield the
    user id; the same payload with one flipped field must not."""
    import time as _t

    from aibroker.auth_session import verify_telegram_widget
    from aibroker.config import get_settings
    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "111:test-token")
    data = _signed_widget_data("111:test-token", auth_date=int(_t.time()))
    assert verify_telegram_widget(data) == 42
    tampered = {**data, "first_name": "evil"}
    assert verify_telegram_widget(tampered) is None


def test_widget_rejects_stale_auth_date(monkeypatch):
    """A perfectly-signed but day-old login must be rejected (replay window)."""
    import time as _t

    from aibroker.auth_session import TG_AUTH_FRESHNESS_S, verify_telegram_widget
    from aibroker.config import get_settings
    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "111:test-token")
    stale = int(_t.time()) - TG_AUTH_FRESHNESS_S - 60
    data = _signed_widget_data("111:test-token", auth_date=stale)
    assert verify_telegram_widget(data) is None
