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
        for model in m["models"].values():
            assert model.startswith(p + "/") or "/" in model


def test_dashboard_pages_are_no_store():
    """Admin pages must send Cache-Control: no-store so Chrome never serves a
    stale key list (regression: showed 77 keys after DB had 51)."""
    # login (anon)
    r = client.get("/login")
    assert "no-store" in r.headers.get("cache-control", "")
    # dashboard (authed)
    r = client.get("/dashboard", cookies=_logged_in_cookies(),
                    follow_redirects=False)
    assert "no-store" in r.headers.get("cache-control", "")


def test_login_page_links_to_favicon():
    r = client.get("/login")
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in r.text


def test_dashboard_html_links_to_favicon():
    """_dash_html wrapper used by /dashboard + /dashboard/projects/{id} drill-down."""
    from aibroker.routes.dashboard import _dash_html
    html = _dash_html(body="<p>x</p>")
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in html


def test_login_page_has_no_literal_double_braces():
    """Regression: _LOGIN_HTML is rendered via .replace() not .format() —
    leftover `{{`/`}}` from f-string template would break CSS + JS in the
    browser (CSS rule silently dropped, JS SyntaxError on the IIFE,
    Telegram widget button never shown).
    Bug observed 2026-06-28: dashboard.py:75 had `body {{ ... }}` etc.
    """
    r = client.get("/login")
    body = r.text
    assert "{{" not in body, "literal {{ leaked from f-string template"
    assert "}}" not in body, "literal }} leaked from f-string template"


def test_login_page_telegram_widget_well_formed():
    """Defensive check: widget <script> tag carries the 4 required data-* attrs."""
    r = client.get("/login")
    for attr in ("data-telegram-login=", "data-size=", "data-radius=",
                  "data-auth-url=", "telegram-widget.js"):
        assert attr in r.text, f"missing {attr} in login HTML"


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


def test_parse_date_range_defaults_to_all_time_when_both_missing():
    """Empty inputs ⇒ (None, None) so _gather_data drops the WHERE clause."""
    from aibroker.routes.dashboard import _parse_date_range
    assert _parse_date_range(None, None) == (None, None)
    assert _parse_date_range("", "") == (None, None)


def test_parse_date_range_parses_valid_iso():
    from datetime import date

    from aibroker.routes.dashboard import _parse_date_range
    df, dt = _parse_date_range("2026-06-01", "2026-06-10")
    assert df == date(2026, 6, 1) and dt == date(2026, 6, 10)


def test_parse_date_range_swaps_inverted_range():
    """If user passes from>to, swap them rather than throw."""
    from datetime import date

    from aibroker.routes.dashboard import _parse_date_range
    df, dt = _parse_date_range("2026-06-10", "2026-06-01")
    assert df == date(2026, 6, 1) and dt == date(2026, 6, 10)


def test_parse_date_range_falls_back_on_garbage_when_partial():
    """Garbage on one side becomes today; swap then puts the older one first."""
    from datetime import UTC, date, datetime

    from aibroker.routes.dashboard import _parse_date_range
    today = datetime.now(UTC).date()
    # garbage 'from' → today; given to=2026-06-10 (older than today) → swap
    df, dt = _parse_date_range("not-a-date", "2026-06-10")
    assert {df, dt} == {today, date(2026, 6, 10)}
    assert df <= dt
    # mirror
    df, dt = _parse_date_range("2026-06-10", "also-not")
    assert {df, dt} == {today, date(2026, 6, 10)}
    assert df <= dt


def test_parse_date_range_one_sided_inputs():
    from datetime import UTC, date, datetime

    from aibroker.routes.dashboard import _parse_date_range
    today = datetime.now(UTC).date()
    # only from → to = today
    df, dt = _parse_date_range("2026-06-01", None)
    assert df == date(2026, 6, 1) and dt == today
    # only to → from = today (and swapped if needed; today > 2026-06-01 so swap happens)
    df, dt = _parse_date_range(None, "2026-06-01")
    assert dt == today


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
    from datetime import UTC, datetime

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
            Recent(datetime(2026, 6, 26, 12, 0, i, tzinfo=UTC),
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


def _fake_main_data(projects=(), keys=(), *, proj_spend: dict | None = None,
                     range_spend: float = 0.0123, range_calls: int = 42,
                     date_from=None, date_to=None,
                     tokens_today: dict | None = None):
    """date_from/date_to default to None = all-time view (no date filter)."""
    return {
        "projects": list(projects),
        "keys": list(keys),
        "date_from": date_from,
        "date_to": date_to,
        "range_spend": range_spend,
        "range_calls": range_calls,
        "range_tin": 12345,
        "range_tout": 6789,
        "proj_spend": proj_spend or {},
        "tokens_today": tokens_today or {},
        "calls_1h": 7,
        "provider_summary": [("cerebras", 5, 0, 5), ("gemini", 3, 1, 4)],
    }


def test_main_render_with_empty_db():
    from aibroker.routes.dashboard import _render
    r = _render(_fake_main_data())
    body = r.body.decode()
    # KPI cards present (new range-driven labels)
    assert "Spend (" in body and "Calls (" in body
    assert "Tokens in / out" in body
    assert "12,345" in body and "6,789" in body  # comma-formatted tokens
    # Date range form present, empty values when all-time
    assert 'name="from"' in body and 'name="to"' in body
    assert 'value=""' in body
    # all-time pill active by default
    assert 'range-reset active' in body
    # All-time label rendered (EN literal in default-EN paint)
    assert "Spend (all time)" in body
    # provider summary line
    assert "cerebras" in body and "gemini" in body
    # tables exist with headers
    assert ">id<" in body and ">provider<" in body
    # sortable JS markers
    assert 'class="sortable"' in body
    # bilingual toggle
    assert 'data-lang="en"' in body and 'data-lang="ru"' in body
    # Totals rows
    assert "<tfoot>" in body
    assert "TOTAL" in body


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


def test_main_render_keys_show_request_axis_when_dominant():
    """Gemini is request-metered (no token quota) — shows req axis."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    # gemini 1500 RPD. 750 req → 50% (token axis disabled for gemini).
    k = ApiKeyRow(
        id=7, provider="gemini", label="t", tier="free",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=750,
    )
    body = _render(_fake_main_data(keys=[k])).body.decode()
    assert "750/1500" in body
    assert "style='width:50%'" in body
    assert "data-sort='50'" in body


def test_main_render_keys_show_token_axis_when_dominant():
    """Cerebras: 525 req (~4 % of 14400) vs 1.36M tok (>100 % of 1M)
    — bar takes token axis, hits red."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    k = ApiKeyRow(
        id=10, provider="cerebras", label="shaboldas1", tier="free",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=525,
    )
    body = _render(_fake_main_data(
        keys=[k],
        tokens_today={10: {"tot": 1_356_576, "tin": 1_200_000, "tout": 156_576}},
    )).body.decode()
    assert "tok" in body                     # token-axis label
    assert "fill bad" in body                # red (≥90%)
    assert "style='width:100%'" in body      # clamped
    # tooltip exposes in/out for debugging
    assert "525 req" in body
    assert "1,200,000 in" in body and "156,576 out" in body


def test_main_render_corp_gemini_output_axis_saturates():
    """Corp Gemini key: 3M in / 80k out manual caps. 76k out (95%) turns the
    bar red even though input (1.5M of 3M) is only 50%."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    k = ApiKeyRow(
        id=11, provider="gemini", label="corp", tier="free",
        scopes=["llm:chat", "llm:edit"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=10,
        manual_tok_in_limit=3_000_000, manual_tok_out_limit=80_000,
    )
    body = _render(_fake_main_data(
        keys=[k],
        tokens_today={11: {"tot": 1_576_000, "tin": 1_500_000, "tout": 76_000}},
    )).body.decode()
    assert "out" in body                     # output axis label shown
    assert "76k/80k out" in body
    assert "fill bad" in body                # 95% → red
    assert "· manual'" in body               # source tag = manual


def test_main_render_keys_paid_provider_no_bar():
    """Paid providers (no quota) just show the count, no bar, sort sentinel -1."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    k = ApiKeyRow(
        id=8, provider="anthropic", label="t", tier="paid",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=42,
    )
    body = _render(_fake_main_data(keys=[k])).body.decode()
    assert ">42<" in body
    assert "data-sort='-1'" in body


def test_dashboard_edit_key_saves_manual_quota_override():
    """Form posts the 4 manual limits; handler persists them, blank → None."""
    r = client.post(
        "/dashboard/keys/99999/edit",
        cookies=_logged_in_cookies(),
        data={"label": "x", "tier": "free", "scopes": ["llm:chat"],
              "manual_tok_in_limit": "3000000", "manual_tok_out_limit": "80000"},
        follow_redirects=False,
    )
    # 303 (key missing) but the form parsed the manual fields without error
    assert r.status_code == 303
    assert "Key+not+found" in r.headers["location"]


def test_keys_table_header_renamed_daily_pct():
    """Column header should read 'daily %' (not 'used') after this change."""
    from aibroker.routes.dashboard import _render
    body = _render(_fake_main_data()).body.decode()
    assert 'data-en="daily %"' in body
    assert 'data-ru="% дня"' in body


def test_main_render_keys_totals_row():
    """tfoot must sum the daily_used, daily_cost_used_usd, error_count cells."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    keys = [
        ApiKeyRow(id=1, provider="cerebras", label="a", tier="free",
                   scopes=["llm:chat"], token_encrypted="x",
                   is_active=True, is_alive=True,
                   daily_used=120, daily_cost_used_usd=0.0,
                   daily_cost_cap_usd=2.0, error_count=1),
        ApiKeyRow(id=2, provider="cerebras", label="b", tier="paid",
                   scopes=["llm:chat"], token_encrypted="x",
                   is_active=True, is_alive=True,
                   daily_used=380, daily_cost_used_usd=0.123,
                   daily_cost_cap_usd=5.0, error_count=3),
    ]
    body = _render(_fake_main_data(keys=keys)).body.decode()
    # 120 + 380 = 500 used
    assert ">500<" in body or ">500 <" in body
    # 0 + 0.123 = $0.1230, cap 2 + 5 = $7.00
    assert "$0.1230 / $7.00" in body
    # error totals 1 + 3
    assert ">4<" in body or ">4 <" in body
    # 2/2 alive
    assert "2 alive" in body


def test_main_render_projects_spend_in_range_column():
    """Each project row shows its spend in the active range; total = sum."""
    from aibroker.db.models import ProjectRow
    from aibroker.routes.dashboard import _render
    p1 = ProjectRow(id=2, name="vera", project_key_prefix="aib_prj_a",
                     project_key_hash="h", allowed_scopes=["llm:chat"],
                     is_active=True, daily_cost_cap_usd=10.0, notes="")
    p2 = ProjectRow(id=3, name="stepan", project_key_prefix="aib_prj_b",
                     project_key_hash="h", allowed_scopes=["llm:chat"],
                     is_active=True, daily_cost_cap_usd=5.0, notes="")
    body = _render(_fake_main_data(
        projects=[p1, p2], proj_spend={2: 9.2798, 3: 0.5910}
    )).body.decode()
    assert "$9.2798" in body and "$0.5910" in body
    # Project totals row
    assert "$15.00" in body                  # cap sum
    assert f"${9.2798 + 0.5910:.4f}" in body  # spend sum


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
