"""routes/dashboard — login, dashboard, form handlers."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aibroker.auth_session import COOKIE_NAME, issue_session_cookie
from aibroker.config import get_settings
from aibroker.main import app


client = TestClient(app)
ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


def _logged_in_cookies(uid: int | None = None) -> dict[str, str]:
    uid = uid or get_settings().OWNER_TELEGRAM_ID or 169510539
    cookie, _ = issue_session_cookie(uid)
    return {COOKIE_NAME: cookie}


# ─── Login page ─────────────────────────────────────────────────────────────


def test_login_page_renders():
    r = client.get("/login")
    assert r.status_code == 200
    assert "AIbroker" in r.text
    assert "telegram-widget" in r.text


def test_login_page_shows_error_param():
    r = client.get("/login?error=Bad+sig")
    assert r.status_code == 200
    assert "Bad" in r.text and "sig" in r.text


# ─── TG widget callback ─────────────────────────────────────────────────────


def test_tg_login_invalid_sig_redirects_to_login():
    r = client.get(
        "/api/tg_login?id=999&hash=deadbeef&auth_date=0",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_tg_login_wrong_user_id_denied():
    """Even if signature were valid, only OWNER_TELEGRAM_ID passes."""
    r = client.get(
        "/api/tg_login?id=12345&hash=deadbeef&auth_date=0",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


# ─── Dashboard requires auth ────────────────────────────────────────────────


def test_dashboard_redirects_without_auth():
    r = client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


@pytest.mark.skipif(
    ON_SQLITE,
    reason="dashboard queries use Postgres-only now() / FILTER",
)
def test_dashboard_renders_with_admin_key():
    r = client.get("/dashboard", headers={"X-Admin-Key": get_settings().ADMIN_KEY})
    assert r.status_code == 200
    assert "AIbroker" in r.text


@pytest.mark.skipif(
    ON_SQLITE,
    reason="dashboard queries use Postgres-only now() / FILTER",
)
def test_dashboard_renders_with_session_cookie():
    r = client.get("/dashboard", cookies=_logged_in_cookies())
    assert r.status_code == 200
    assert "AIbroker" in r.text


# ─── Logout ─────────────────────────────────────────────────────────────────


def test_logout_clears_cookie():
    r = client.get("/logout", cookies=_logged_in_cookies(), follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
    # Cookie cleared via Set-Cookie: ... Max-Age=0
    cookies = r.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookies


# ─── Form handlers (require auth — verify the gate) ─────────────────────────


def test_dashboard_create_key_form_requires_auth():
    r = client.post(
        "/dashboard/keys/create",
        data={"provider": "x", "label": "y", "token": "tok-12345678"},
        follow_redirects=False,
    )
    assert r.status_code == 401   # require_owner_session raises 401


def test_dashboard_disable_key_form_requires_auth():
    r = client.post("/dashboard/keys/42/disable", follow_redirects=False)
    assert r.status_code == 401


def test_dashboard_delete_key_form_requires_auth():
    r = client.post("/dashboard/keys/42/delete", follow_redirects=False)
    assert r.status_code == 401


def test_dashboard_create_project_form_requires_auth():
    r = client.post(
        "/dashboard/projects/create",
        data={"name": "p", "allowed_scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 401


# ─── Edit form handlers (auth + validation) ────────────────────────────────


def test_dashboard_edit_key_requires_auth():
    r = client.post(
        "/dashboard/keys/42/edit",
        data={"label": "x", "tier": "free", "scope": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_dashboard_edit_project_requires_auth():
    r = client.post(
        "/dashboard/projects/42/edit",
        data={"name": "x", "allowed_scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 401


def test_dashboard_edit_key_with_session_rejects_bad_tier():
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "lifetime", "scope": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Bad+tier" in r.headers["location"]


def test_dashboard_edit_key_with_session_rejects_bad_scope():
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "free", "scope": "admin:write"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Bad+scope" in r.headers["location"]


def test_dashboard_edit_key_404_when_missing():
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "free", "scope": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Key+not+found" in r.headers["location"]


def test_dashboard_edit_project_rejects_empty_scopes():
    r = client.post(
        "/dashboard/projects/99999/edit",
        cookies=_logged_in_cookies(),
        data={"name": "x", "allowed_scopes": "   ,  ,"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Need+at+least+one+scope" in r.headers["location"]


def test_dashboard_edit_project_404_when_missing():
    r = client.post(
        "/dashboard/projects/99999/edit",
        cookies=_logged_in_cookies(),
        data={"name": "x", "allowed_scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Project+not+found" in r.headers["location"]
