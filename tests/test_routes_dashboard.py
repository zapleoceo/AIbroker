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


def test_positive_int_or_none():
    from aibroker.routes.dashboard import _positive_int_or_none
    assert _positive_int_or_none("3000000") == 3_000_000
    assert _positive_int_or_none("") is None
    assert _positive_int_or_none("  ") is None
    assert _positive_int_or_none("0") is None       # 0 = no cap, not "block all"
    assert _positive_int_or_none("-5") is None
    assert _positive_int_or_none("abc") is None


def test_apply_manual_limits_sets_all_four_axes():
    """Shared helper used by add-create, upsert and edit — parses raw form
    strings into the four manual_* columns (blank/0/garbage → None)."""
    from types import SimpleNamespace

    from aibroker.routes.dashboard import _apply_manual_limits
    key = SimpleNamespace()
    _apply_manual_limits(key, req="500", tok="", tok_in="3000000", tok_out="0")
    assert key.manual_req_limit == 500
    assert key.manual_tok_limit is None        # blank → no cap
    assert key.manual_tok_in_limit == 3_000_000
    assert key.manual_tok_out_limit is None    # 0 → no cap


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL autoincrement needs Postgres")
def test_create_and_edit_key_persist_manual_limits():
    """Full loop: add a key with all 4 manual limits via the dashboard form,
    then edit them, and assert each axis round-trips into the DB. Covers the
    create / upsert / edit persistence branches."""
    import asyncio

    from sqlalchemy import select

    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow

    # 1. create with manual in/out caps (corp-Gemini shape)
    r = client.post(
        "/dashboard/keys/create", cookies=_logged_in_cookies(),
        data={"provider": "gemini", "label": "corp",
              "token": "g-fake-token-1234567890",
              "scopes": ["llm:chat", "llm:vision"],
              "manual_tok_in_limit": "3000000", "manual_tok_out_limit": "80000"},
        follow_redirects=False,
    )
    assert r.status_code == 303

    async def _read():
        async with get_session() as s:
            return (await s.execute(
                select(ApiKeyRow).where(ApiKeyRow.label == "corp")
            )).scalar_one()

    row = asyncio.get_event_loop().run_until_complete(_read())
    assert row.manual_tok_in_limit == 3_000_000
    assert row.manual_tok_out_limit == 80_000
    assert row.manual_req_limit is None      # blank → no cap

    # 2. edit: tighten req cap, clear the out cap
    r = client.post(
        f"/dashboard/keys/{row.id}/edit", cookies=_logged_in_cookies(),
        data={"label": "corp", "tier": "free", "scopes": ["llm:chat"],
              "manual_req_limit": "500", "manual_tok_out_limit": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row2 = asyncio.get_event_loop().run_until_complete(_read())
    assert row2.manual_req_limit == 500
    assert row2.manual_tok_out_limit is None   # cleared


def test_add_key_form_has_four_manual_limit_fields():
    """The ADD-key form must expose all four optional quota overrides
    (regression: it only had the $ cost cap)."""
    from aibroker.routes.dashboard import _render
    body = _render(_fake_main_data()).body.decode()
    # add-key form id present
    assert 'id="add-key-form"' in body
    for field in ("manual_req_limit", "manual_tok_limit",
                   "manual_tok_in_limit", "manual_tok_out_limit"):
        assert f'name="{field}"' in body, f"add form missing {field}"


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


def test_login_page_is_no_store():
    """Admin pages must send Cache-Control: no-store so Chrome never serves a
    stale snapshot (regression: dashboard showed 77 keys after DB had 51).
    /login needs no DB so it's the SQLite-safe proxy for the header wiring;
    /dashboard + drill-down share the same _NO_STORE constant."""
    r = client.get("/login")
    assert "no-store" in r.headers.get("cache-control", "")


def test_no_store_constant_applied_to_renders():
    """_render and _render_project_detail must carry the no-store header
    (they hit Postgres-only SQL via the route, so assert at the unit level)."""
    from aibroker.routes.dashboard import _render
    resp = _render(_fake_main_data())
    assert "no-store" in resp.headers.get("cache-control", "")


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
                       providers: list[tuple] | None = None,
                       lat_hist: list[int] | None = None):
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
        "lat_hist": lat_hist if lat_hist is not None else [1, 2, 0, 1, 0, 0, 0, 0],
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
    # Calls KPI card shows the ok/err split (Status mix tile was removed —
    # it duplicated this exact split via a second query)
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


def test_render_project_detail_no_status_mix_tile():
    """Status mix tile was removed — usage_log only ever has status
    ok/error, so it duplicated the Calls KPI card's ok/err split via a
    second query. The split still lives in the KPI card only."""
    from aibroker.routes.dashboard import _render_project_detail
    body = _render_project_detail(_fake_proj_detail()).body.decode()
    assert "Status mix" not in body
    assert "Статусы" not in body


def test_render_project_detail_handles_empty_breakdowns():
    from aibroker.routes.dashboard import _render_project_detail
    d = _fake_proj_detail()
    d["by_provider"] = []
    d["by_capability"] = []
    d["by_model"] = []
    body = _render_project_detail(d).body.decode()
    # Empty breakdown card falls back to the no-data line (bilingual)
    assert "(no data in this range)" in body


# ─── Latency histogram ───────────────────────────────────────────────────────


def test_lat_labels_align_with_edges():
    """width_bucket yields len(edges)+1 buckets — labels must match 1:1."""
    from aibroker.routes.dashboard import _LAT_EDGES_MS, _LAT_LABELS
    assert len(_LAT_LABELS) == len(_LAT_EDGES_MS) + 1


def test_lat_hist_counts_maps_sparse_to_dense():
    """width_bucket returns only non-empty buckets; missing ones become 0."""
    from collections import namedtuple

    from aibroker.routes.dashboard import _LAT_LABELS, _lat_hist_counts
    Row = namedtuple("Row", "b n")
    counts = _lat_hist_counts([Row(0, 5), Row(3, 2), Row(7, 1)])
    assert len(counts) == len(_LAT_LABELS)
    assert counts[0] == 5 and counts[3] == 2 and counts[7] == 1
    assert counts[1] == 0 and counts[6] == 0


def test_render_project_detail_shows_latency_histogram():
    from aibroker.routes.dashboard import _render_project_detail
    # busiest bucket (10) → full-width bar; empty buckets → 0%.
    d = _fake_proj_detail(lat_hist=[10, 5, 0, 0, 0, 0, 0, 0])
    body = _render_project_detail(d).body.decode()
    assert "Latency distribution" in body
    assert "&lt;250ms" in body or "<250ms" in body   # first bucket label
    assert "style='width:100%'" in body or 'style="width:100%"' in body  # peak bar
    assert ">10<" in body and ">5<" in body           # per-bucket counts


def test_render_project_detail_hides_empty_histogram():
    from aibroker.routes.dashboard import _render_project_detail
    d = _fake_proj_detail(lat_hist=[0] * 8)
    body = _render_project_detail(d).body.decode()
    assert "Latency distribution" not in body


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
    """Gemini is request-metered (no token quota) — shows the req axis chip."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    # gemini 1500 RPD. 750 req → 50% (token axis disabled for gemini).
    k = ApiKeyRow(
        id=7, provider="gemini", label="t", tier="free",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=750,
    )
    body = _render(_fake_main_data(keys=[k])).body.decode()
    assert "50% req" in body                 # chip
    assert "750/1,500 req" in body           # tooltip used/cap
    assert "style='width:50%'" in body
    assert "data-sort='50'" in body


def test_main_render_keys_show_both_axes_for_groq():
    """Groq has BOTH req + tok caps — both chips shown, tok dominant.
    Demonstrates same-cap visibility: tooltip spells out used/cap per axis."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    k = ApiKeyRow(
        id=10, provider="groq", label="shaboldas1", tier="free",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=525,
    )
    body = _render(_fake_main_data(
        keys=[k],
        tokens_today={10: {"tot": 678_288, "tin": 600_000, "tout": 78_288}},
    )).body.decode()
    assert "100% tok" in body                # token axis dominant (clamped)
    assert "3% req" in body                  # request axis also shown
    assert "fill bad" in body                # red (≥90%)
    assert "style='width:100%'" in body
    # tooltip spells out used/cap on both axes (default groq caps)
    assert "678,288/500,000 tok" in body
    assert "525/14,400 req" in body


def test_main_render_cerebras_token_axis_only():
    """Cerebras is token-metered — its req-day header isn't a hard cap, so the
    req axis is dropped. Only the tok chip shows (no req chip)."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    k = ApiKeyRow(
        id=10, provider="cerebras", label="shaboldas1", tier="free",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=5_000,
    )
    body = _render(_fake_main_data(
        keys=[k],
        tokens_today={10: {"tot": 500_000, "tin": 450_000, "tout": 50_000}},
    )).body.decode()
    assert "50% tok" in body                 # 500k / 1M cerebras tok cap
    assert "500,000/1,000,000 tok" in body
    assert "% req" not in body               # no request axis at all
    assert "/14,400 req" not in body


def test_main_render_corp_gemini_output_axis_saturates():
    """Corp Gemini key: 3M in / 80k out manual caps. 76k out (95%) is the
    dominant chip + red bar even though input (1.5M of 3M) is only 50%."""
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
    assert "95% out" in body                 # output axis dominant chip
    assert "50% in" in body                  # input axis also shown
    assert "76,000/80,000 out" in body       # tooltip used/cap
    assert "fill bad" in body                # 95% → red
    assert "style='width:95%'" in body
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


def test_main_render_paid_key_no_free_tier_quota_bar():
    """A paid gemini key isn't bound by the 1,500 free RPD seed — the quota
    column shows just the count, not a misleading 200%+ req bar. Its $/day cap
    is rendered in the separate cost column."""
    from aibroker.db.models import ApiKeyRow
    from aibroker.routes.dashboard import _render
    k = ApiKeyRow(
        id=16, provider="gemini", label="demoniwwwe", tier="paid",
        scopes=["llm:chat"], token_encrypted="x",
        is_active=True, is_alive=True, daily_used=3183,
        daily_cost_cap_usd=1.0, daily_cost_used_usd=0.42,
    )
    body = _render(_fake_main_data(keys=[k])).body.decode()
    assert "% req" not in body                # no free-tier req axis for paid
    assert ">3183<" in body                   # quota column = plain count
    assert "$0.4200 / $1.00" in body          # separate cost-cap column


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


def test_tables_have_row_number_column():
    """Both tables show a '#' row-number column (CSS counter) so the visible
    count isn't confused with the DB id (which has gaps from deletions —
    e.g. 51 rows but max id 77)."""
    from aibroker.db.models import ApiKeyRow, ProjectRow
    from aibroker.routes.dashboard import _render
    k = ApiKeyRow(id=77, provider="cerebras", label="t", tier="free",
                   scopes=["llm:chat"], token_encrypted="x",
                   is_active=True, is_alive=True, daily_used=0)
    p = ProjectRow(id=4, name="stepan2", project_key_prefix="aib_prj_x",
                    project_key_hash="h", allowed_scopes=["llm:chat"],
                    is_active=True, notes="")
    body = _render(_fake_main_data(keys=[k], projects=[p])).body.decode()
    # # header in both tables (counter renders the actual number via CSS)
    assert "<th>#</th>" in body
    # each data row carries the rownum cell
    assert '<td class="rownum"></td>' in body
    # CSS counter wired
    assert "counter-increment: rownum" in body
    assert "counter(rownum)" in body
    # id column still present (id 77 shown literally, distinct from row #)
    assert 'data-sort="77"' in body


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
