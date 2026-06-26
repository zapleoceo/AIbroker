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


def test_validate_scope_list_strips_dups_and_empties():
    from aibroker.routes.dashboard import _validate_scope_list
    assert _validate_scope_list(["llm:chat", "llm:edit", "llm:chat"]) == [
        "llm:chat", "llm:edit"
    ]
    assert _validate_scope_list(["llm:chat", "  ", ""]) == ["llm:chat"]


def test_validate_scope_list_rejects_unknown():
    from aibroker.routes.dashboard import _validate_scope_list
    assert _validate_scope_list(["llm:chat", "admin:write"]) is None
    assert _validate_scope_list([]) is None
    assert _validate_scope_list(["", " "]) is None


def test_scope_checkboxes_renders_4_options_with_checked_state():
    from aibroker.routes.dashboard import _scope_checkboxes
    html = _scope_checkboxes(["llm:chat", "llm:edit"])
    # All 4 known scopes rendered
    for s in ("llm:chat", "llm:embed", "llm:vision", "llm:edit"):
        assert f'value="{s}"' in html
    # Only the two selected have `checked`
    assert html.count(" checked") == 2


def test_provider_catalogue_lists_known_providers():
    """Helper that drives the add-key dropdown."""
    from aibroker.routes.dashboard import _provider_catalogue
    cat = _provider_catalogue()
    names = [p["provider"] for p in cat]
    assert "cerebras" in names
    assert "gemini" in names
    assert "voyage" in names
    assert "anthropic" in names
    assert "mistral" in names
    assert "cohere" in names
    # Order: free-first
    assert names.index("cerebras") < names.index("openai")
    assert names.index("cerebras") < names.index("anthropic")
    # voyage is embed-only → default_scope
    voy = next(p for p in cat if p["provider"] == "voyage")
    assert voy["default_scope"] == "llm:embed"
    # chat providers default to llm:chat
    cer = next(p for p in cat if p["provider"] == "cerebras")
    assert cer["default_scope"] == "llm:chat"
    # Every entry exposes its model map
    for p in cat:
        assert p["models"], f"{p['provider']} should have at least one model"


def test_provider_meta_json_is_parseable():
    import json

    from aibroker.routes.dashboard import _provider_meta_json
    meta = json.loads(_provider_meta_json())
    assert "cerebras" in meta
    assert meta["voyage"]["default_scope"] == "llm:embed"
    # Each provider's models is a dict capability → model id
    for p, m in meta.items():
        assert isinstance(m["models"], dict)
        for cap, model in m["models"].items():
            assert model.startswith(p + "/") or "/" in model


def test_login_page_has_lang_toggle():
    r = client.get("/login")
    assert 'data-lang="en"' in r.text
    assert 'data-lang="ru"' in r.text
    assert "Войти через Telegram" in r.text  # RU embedded
    assert "Sign in with Telegram" in r.text  # EN embedded
    assert "localStorage" in r.text


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


def test_parse_scopes_validation():
    """Multi-scope CSV parsing for the key reassignment form."""
    from aibroker.routes.dashboard import _parse_scopes
    assert _parse_scopes("llm:chat,llm:edit") == ["llm:chat", "llm:edit"]
    assert _parse_scopes("  llm:edit  ") == ["llm:edit"]
    assert _parse_scopes("") is None
    assert _parse_scopes("   ,  ,") is None
    assert _parse_scopes("llm:chat,bogus") is None


def test_dashboard_edit_key_requires_auth():
    r = client.post(
        "/dashboard/keys/42/edit",
        data={"label": "x", "tier": "free", "scopes": "llm:chat"},
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
        data={"label": "x", "tier": "lifetime", "scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Bad+tier" in r.headers["location"]


def test_dashboard_edit_key_with_session_rejects_bad_scope():
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "free", "scopes": "admin:write"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    # 2026-06-26: scope form moved from CSV to multi-checkbox; flash unified
    assert "Bad+or+empty+scope" in r.headers["location"]


def test_dashboard_edit_key_with_session_rejects_empty_scopes():
    """No scope checkboxes ticked → rejected (empty scope list)."""
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "free"},  # no 'scopes' key at all
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Bad+or+empty+scope" in r.headers["location"]


def test_dashboard_edit_key_accepts_multiple_scopes():
    """Multi-select form sends `scopes=llm:chat&scopes=llm:edit`."""
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "free",
               "scopes": ["llm:chat", "llm:edit"]},
        follow_redirects=False,
    )
    # 303 because key 99999 doesn't exist, but multi-scope parsed ok
    # (would 303 with Bad+or+empty+scope otherwise)
    assert r.status_code == 303
    assert "Key+not+found" in r.headers["location"]


def test_dashboard_edit_key_404_when_missing():
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "free", "scopes": "llm:chat"},
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


def test_project_detail_requires_auth():
    r = client.get("/dashboard/projects/42", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]


def test_project_detail_404_when_missing():
    r = client.get(
        "/dashboard/projects/99999",
        cookies=_logged_in_cookies(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Project+not+found" in r.headers["location"]


def test_range_hours_table_complete():
    """The 24h/7d/30d range pills must all map to valid hour windows."""
    from aibroker.routes.dashboard import _RANGE_HOURS
    assert _RANGE_HOURS["24h"] == 24
    assert _RANGE_HOURS["7d"] == 168
    assert _RANGE_HOURS["30d"] == 720


# ─── Project-detail rendering (unit, no DB) ────────────────────────────────


def _fake_proj_detail(*, hours: int = 24, recent_n: int = 3,
                       providers: list[tuple] | None = None):
    """Build the dict that _render_project_detail() consumes — no DB."""
    from collections import namedtuple
    from datetime import datetime, timezone
    from aibroker.db.models import ProjectRow

    project = ProjectRow(
        id=7, name="stepan", project_key_prefix="aib_prj_xy",
        project_key_hash="hash", allowed_scopes=["llm:chat", "llm:edit"],
        daily_cost_cap_usd=2.0, is_active=True, owner_email="x@y", notes="",
    )
    Totals = namedtuple("T", "calls spend tin tout avg_lat ok_n err_n")
    Brk = namedtuple("B", "provider n spend")
    BrkCap = namedtuple("BC", "cap n spend")
    BrkModel = namedtuple("BM", "model n spend toks")
    BrkSt = namedtuple("BS", "status n")
    Recent = namedtuple("R", "created_at provider model capability tokens_in "
                              "tokens_out cost_usd latency_ms status http_status "
                              "error_kind")
    return {
        "project": project,
        "hours": hours,
        "totals": Totals(calls=4, spend=0.123, tin=1234, tout=567,
                          avg_lat=345, ok_n=3, err_n=1),
        "by_provider": providers or [Brk("cerebras", 2, 0.0), Brk("gemini", 2, 0.123)],
        "by_capability": [BrkCap("chat:fast", 3, 0.05), BrkCap("chat:edit", 1, 0.07)],
        "by_model": [BrkModel("cerebras/gpt-oss-120b", 2, 0.0, 800)],
        "by_status": [BrkSt("ok", 3), BrkSt("rate_limit", 1)],
        "recent": [
            Recent(datetime(2026, 6, 26, 12, 0, i, tzinfo=timezone.utc),
                    "cerebras", "cerebras/gpt-oss-120b", "chat:fast",
                    100, 50, 0.0, 234, "ok", 200, None)
            for i in range(recent_n)
        ],
    }


def test_render_project_detail_smoke():
    from aibroker.routes.dashboard import _render_project_detail
    r = _render_project_detail(_fake_proj_detail())
    body = r.body.decode()
    # KPI values
    assert "stepan" in body
    assert "$0.1230" in body                 # spend
    assert "1,234" in body and "567" in body  # token counts (formatted with comma)
    assert "345 ms" in body                  # latency
    # Status mix split shown
    assert "3 ok" in body and "1 err" in body
    # Range pills present + 24h active
    assert 'href="?range=24h"' in body
    assert 'href="?range=7d"' in body
    assert 'href="?range=30d"' in body
    assert 'range=24h" class="active"' in body


def test_render_project_detail_sortable_recent_rows():
    """Bug regression: recent table rows must carry 'data-row' for the JS to sort."""
    from aibroker.routes.dashboard import _render_project_detail
    body = _render_project_detail(_fake_proj_detail(recent_n=5)).body.decode()
    assert body.count('tr class="data-row"') >= 5
    # And each cell that drives a sort must expose data-sort
    assert 'data-sort="2026-06-26T12:00:00' in body   # iso8601 time
    assert 'data-sort="150"' in body                   # tokens_in+tokens_out
    assert 'data-sort="234"' in body                   # latency


def test_render_project_detail_handles_no_recent_calls():
    from aibroker.routes.dashboard import _render_project_detail
    body = _render_project_detail(_fake_proj_detail(recent_n=0)).body.decode()
    assert "no calls yet" in body


def test_render_project_detail_handles_empty_breakdowns():
    from aibroker.routes.dashboard import _render_project_detail
    d = _fake_proj_detail()
    d["by_provider"] = []
    d["by_capability"] = []
    d["by_model"] = []
    d["by_status"] = []
    body = _render_project_detail(d).body.decode()
    # Empty breakdown card falls back to the no-data line (bilingual)
    assert "(no data in this range)" in body


# ─── Main dashboard render (unit, no DB) ───────────────────────────────────


def _fake_main_data(projects=(), keys=()):
    return {
        "projects": list(projects),
        "keys": list(keys),
        "spend_today": 0.0123,
        "calls_1h": 42,
        "provider_summary": [("cerebras", 5, 0, 5), ("gemini", 3, 1, 4)],
    }


def test_main_render_with_empty_db():
    from aibroker.routes.dashboard import _render
    r = _render(_fake_main_data())
    body = r.body.decode()
    # KPI cards present
    assert "Spend today" in body and "Calls 1h" in body
    # provider summary line
    assert "cerebras" in body and "gemini" in body
    # tables exist with headers
    assert ">id<" in body and ">provider<" in body
    # flash empty area
    assert 'class="flash"' not in body or "<div " not in body[:200]
    # sortable JS markers
    assert 'class="sortable"' in body
    # bilingual toggle
    assert 'data-lang="en"' in body and 'data-lang="ru"' in body


def test_main_render_renders_key_rows_with_data_row_marker():
    """Each key row needs class='data-row' for the sorter to pick it up."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    fake_key = ApiKeyRow(
        id=1, provider="cerebras", label="t", tier="free",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True,
    )
    body = _render(_fake_main_data(keys=[fake_key])).body.decode()
    assert 'data-row-id="k1"' in body
    # Edit form partner row also present
    assert 'data-edit-for="k1"' in body
    # Scope checkboxes rendered inside the edit row
    assert 'name="scopes" value="llm:chat"' in body


def test_main_render_renders_project_rows_with_drill_link():
    from aibroker.db.models import ProjectRow
    from aibroker.routes.dashboard import _render
    proj = ProjectRow(
        id=7, name="stepan", project_key_prefix="aib_prj_xy",
        project_key_hash="hash", allowed_scopes=["llm:chat"],
        is_active=True, notes="",
    )
    body = _render(_fake_main_data(projects=[proj])).body.decode()
    assert '<a href="/dashboard/projects/7"' in body
    assert 'data-row-id="p7"' in body
    assert 'data-edit-for="p7"' in body


def test_main_render_shows_new_project_key_flash():
    """When a project is created the one-time key is rendered prominently."""
    from aibroker.routes.dashboard import _render
    body = _render(
        _fake_main_data(), new_project_key="aib_prj_ABCDEFGH"
    ).body.decode()
    assert "aib_prj_ABCDEFGH" in body
    assert "SAVE this key now" in body


def test_dashboard_edit_project_404_when_missing():
    r = client.post(
        "/dashboard/projects/99999/edit",
        cookies=_logged_in_cookies(),
        data={"name": "x", "allowed_scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Project+not+found" in r.headers["location"]
