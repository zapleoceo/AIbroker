"""Self-signup anti-bot guards: proof of work, Telegram verification, email-domain rules."""
from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.main import app
from aibroker.services import signup_guard
from aibroker.services.signup_guard import (
    PowInvalid,
    check_pow,
    email_domain,
    is_disposable,
    issue_challenge,
    leading_zero_bits,
    solve,
)

client = TestClient(app)
_TOKEN = "123456:test-bot-token"


@pytest.fixture(autouse=True)
def _guard_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "SIGNUP_POW_BITS", 8)          # fast tests, same code path
    monkeypatch.setattr(s, "TELEGRAM_BOT_TOKEN", _TOKEN)
    monkeypatch.delenv("REDIS_URL", raising=False)
    signup_guard._USED_LOCAL.clear()


def _pow() -> dict[str, str]:
    ch = issue_challenge()
    return {"challenge": ch["challenge"], "nonce": solve(ch["challenge"], ch["difficulty_bits"])}


def _tg(user_id: int = 4242, **over) -> dict[str, str]:
    data = {"id": str(user_id), "first_name": "Ann", "username": "ann",
            "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hashlib.sha256(_TOKEN.encode()).digest()
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return {**data, **over}


def _signup(ip: int = 1, **body):
    return client.post("/v1/signup", json={"name": "guard-bot", **body},
                       headers={"X-Forwarded-For": f"198.51.100.{ip}"})


def test_leading_zero_bits_and_solver():
    assert leading_zero_bits(b"\x00\x0f") == 12 and leading_zero_bits(b"\x80") == 0
    ch = issue_challenge()["challenge"]
    nonce = solve(ch, 8)
    assert leading_zero_bits(hashlib.sha256(f"{ch}:{nonce}".encode()).digest()) >= 8


async def test_check_pow_rejects_missing_forged_expired_weak_and_reused():
    with pytest.raises(PowInvalid, match="required"):
        await check_pow(None, None)
    good = _pow()
    forged = good["challenge"].rsplit(".", 1)[0] + ".deadbeef"
    with pytest.raises(PowInvalid, match="not issued"):
        await check_pow(forged, good["nonce"])
    old = issue_challenge(now=time.time() - 3600)["challenge"]
    with pytest.raises(PowInvalid, match="expired"):
        await check_pow(old, solve(old, 8))
    weak = issue_challenge()["challenge"]
    bad_nonce = next(str(n) for n in range(10_000) if leading_zero_bits(
        hashlib.sha256(f"{weak}:{n}".encode()).digest()) < 8)
    with pytest.raises(PowInvalid, match="leading zero bits"):
        await check_pow(weak, bad_nonce)
    await check_pow(good["challenge"], good["nonce"])
    with pytest.raises(PowInvalid, match="already used"):
        await check_pow(good["challenge"], good["nonce"])


def test_challenge_endpoint():
    body = client.get("/v1/signup/challenge").json()
    assert body["difficulty_bits"] == 8 and body["expires_in_s"] == 600
    assert body["challenge"].count(".") == 2


def test_signup_without_pow_is_400():
    r = _signup()
    assert r.status_code == 400 and r.json()["error"] == "pow_invalid"


async def test_unverified_signup_gets_the_small_cap():
    r = _signup(pow=_pow())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is False and body["verify_url"].endswith("/signup#verify")
    assert body["limits"]["total_request_cap"] == get_settings().SIGNUP_UNVERIFIED_REQUEST_CAP
    async with get_session() as s:
        row = (await s.execute(select(ProjectRow).where(ProjectRow.name == body["name"]))
               ).scalar_one()
    assert row.signup_verified_at is None and row.signup_telegram_id is None


async def test_telegram_signup_is_verified_with_the_full_cap():
    r = _signup(pow=_pow(), telegram=_tg(7))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified"] is True and body["verify_url"] == ""
    assert body["limits"]["total_request_cap"] == get_settings().SIGNUP_REQUEST_CAP
    async with get_session() as s:
        row = (await s.execute(select(ProjectRow).where(ProjectRow.name == body["name"]))
               ).scalar_one()
    assert row.signup_telegram_id == 7 and row.signup_verified_at is not None


def test_forged_telegram_payload_is_400():
    r = _signup(pow=_pow(), telegram=_tg(8, hash="0" * 64))
    assert r.status_code == 400 and r.json()["error"] == "telegram_invalid"


async def test_projects_per_telegram_account_are_limited(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_PER_TELEGRAM", 1)
    assert _signup(ip=2, pow=_pow(), telegram=_tg(9)).status_code == 200
    r = _signup(ip=3, pow=_pow(), telegram=_tg(9))
    assert r.status_code == 429 and r.json()["scope"] == "telegram"


async def test_verify_endpoint_lifts_the_cap_once():
    key = _signup(ip=4, pow=_pow()).json()["project_key"]
    h = {"X-Project-Key": key}
    r = client.post("/v1/signup/verify", headers=h, json={"telegram": _tg(10)})
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True
    assert r.json()["limits"]["total_request_cap"] == get_settings().SIGNUP_REQUEST_CAP
    again = client.post("/v1/signup/verify", headers=h, json={"telegram": _tg(10)})
    assert again.status_code == 409 and again.json()["error"] == "not_eligible"
    forged = client.post("/v1/signup/verify", headers=h, json={"telegram": _tg(10, hash="x")})
    assert forged.status_code == 400


def test_disposable_email_is_refused():
    assert email_domain("Dev@Mailinator.com") == "mailinator.com"
    assert email_domain("@handle") is None or email_domain("@handle") == "handle"
    assert is_disposable("mailinator.com") and is_disposable("x.yopmail.com")
    assert not is_disposable("gmail.com")
    r = _signup(ip=5, pow=_pow(), contact="bot@mailinator.com")
    assert r.status_code == 422 and r.json()["error"] == "disposable_email"


async def test_signups_per_email_domain_are_limited(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_PER_EMAIL_DOMAIN_PER_DAY", 1)
    assert _signup(ip=6, pow=_pow(), contact="a@corp.example").status_code == 200
    r = _signup(ip=7, pow=_pow(), contact="b@CORP.example")
    assert r.status_code == 429 and r.json()["scope"] == "email_domain"


def test_signup_page_serves_the_form():
    r = client.get("/signup")
    assert r.status_code == 200
    assert "telegram-widget.js" in r.text and "/v1/signup/challenge" in r.text
