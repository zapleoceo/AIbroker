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

    # 1. create with manual in/out caps (corp-Gemini shape) + a cloudflare-style
    # account_id, to prove that field round-trips too.
    r = client.post(
        "/dashboard/keys/create", cookies=_logged_in_cookies(),
        data={"provider": "gemini", "label": "corp",
              "token": "g-fake-token-1234567890",
              "scopes": ["llm:chat", "llm:vision"],
              "manual_tok_in_limit": "3000000", "manual_tok_out_limit": "80000",
              "account_id": "acct-123"},
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
    assert row.account_id == "acct-123"

    # 2. edit: tighten req cap, clear the out cap, change account_id
    r = client.post(
        f"/dashboard/keys/{row.id}/edit", cookies=_logged_in_cookies(),
        data={"label": "corp", "tier": "free", "scopes": ["llm:chat"],
              "manual_req_limit": "500", "manual_tok_out_limit": "",
              "account_id": "acct-456"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    row2 = asyncio.get_event_loop().run_until_complete(_read())
    assert row2.manual_req_limit == 500
    assert row2.manual_tok_out_limit is None   # cleared
    assert row2.account_id == "acct-456"


def test_validate_scope_list_strips_dups_and_empties():
    from aibroker.routes.dashboard_scopes import _validate_scope_list
    assert _validate_scope_list(["llm:chat", "llm:edit", "llm:chat"]) == [
        "llm:chat", "llm:edit"
    ]
    assert _validate_scope_list(["llm:chat", "  ", ""]) == ["llm:chat"]


def test_validate_scope_list_rejects_unknown():
    from aibroker.routes.dashboard_scopes import _validate_scope_list
    assert _validate_scope_list(["llm:chat", "admin:write"]) is None
    assert _validate_scope_list([]) is None
    assert _validate_scope_list(["", " "]) is None


def test_scope_checkboxes_renders_4_options_with_checked_state():
    from aibroker.routes.dashboard_scopes import _scope_checkboxes
    html = _scope_checkboxes(["llm:chat", "llm:edit"])
    # All 4 known scopes rendered
    for s in ("llm:chat", "llm:embed", "llm:vision", "llm:edit"):
        assert f'value="{s}"' in html
    # Only the two selected have `checked`
    assert html.count(" checked") == 2


def test_login_page_is_no_store():
    """Admin pages must send Cache-Control: no-store so Chrome never serves a
    stale snapshot (regression: dashboard showed 77 keys after DB had 51).
    /login needs no DB so it's the SQLite-safe proxy for the header wiring;
    /dashboard + drill-down share the same _NO_STORE constant."""
    r = client.get("/login")
    assert "no-store" in r.headers.get("cache-control", "")


def test_login_page_links_to_favicon():
    r = client.get("/login")
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in r.text


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


# ─── _gather_data perf refactor (merged scan, sargable ranges, gather) ──────


def test_range_where_no_filter_returns_empty():
    from aibroker.routes.dashboard_data import _range_where
    where, bind_ = _range_where(None, None)
    assert where == "" and bind_ == {}


def test_range_where_both_sides_is_sargable_half_open():
    from datetime import date, datetime, timedelta

    from aibroker.routes.dashboard_data import _range_where
    df, dt = date(2026, 6, 1), date(2026, 6, 3)
    where, bind_ = _range_where(df, dt)
    assert "::date" not in where            # non-sargable cast must be gone
    assert "created_at >=" in where and "created_at <" in where
    assert bind_["start"] == datetime(2026, 6, 1)
    assert bind_["end"] == datetime(2026, 6, 4)   # dt + 1 day, exclusive upper
    assert bind_["end"] - bind_["start"] == timedelta(days=3)  # 3 whole days inclusive


def test_range_where_from_only_open_ended():
    from datetime import date, datetime

    from aibroker.routes.dashboard_data import _range_where
    where, bind_ = _range_where(date(2026, 6, 1), None)
    assert where == "WHERE created_at >= :start"
    assert bind_ == {"start": datetime(2026, 6, 1)}


def test_range_where_to_only_exclusive_next_day():
    from datetime import date, datetime

    from aibroker.routes.dashboard_data import _range_where
    where, bind_ = _range_where(None, date(2026, 6, 1))
    assert where == "WHERE created_at < :end"
    assert bind_ == {"end": datetime(2026, 6, 2)}   # inclusive of the whole day


def test_fetch_range_and_proj_spend_merges_one_scan():
    """Regression: range totals + proj_spend used to be two separate
    full-table SUMs; now one GROUP BY project_id scan feeds both (portable
    SQL — no Postgres-only functions, so this runs under SQLite too). Seed
    rows across two projects plus one project-less row and assert both
    views agree."""
    import asyncio

    from aibroker.db import get_session
    from aibroker.db.models import ProjectRow, UsageLogRow
    from aibroker.routes.dashboard_data import _fetch_range_and_proj_spend

    # Explicit ids: BigInteger PKs don't autoincrement under SQLite/aiosqlite
    # (same reason other tests skipif ON_SQLITE) — supplying ids sidesteps
    # that and keeps this test genuinely portable, since the SQL itself
    # (GROUP BY project_id, no Postgres-only functions) is.
    async def _seed():
        async with get_session() as s:
            s.add_all([
                ProjectRow(id=901, name="p-alpha", project_key_prefix="aib_prj_a",
                            project_key_hash="ha", allowed_scopes=["llm:chat"],
                            is_active=True, notes=""),
                ProjectRow(id=902, name="p-beta", project_key_prefix="aib_prj_b",
                            project_key_hash="hb", allowed_scopes=["llm:chat"],
                            is_active=True, notes=""),
                UsageLogRow(id=910, project_id=901, provider="gemini", tokens_in=100,
                             tokens_out=50, cost_usd=1.5, status="ok"),
                UsageLogRow(id=911, project_id=901, provider="gemini", tokens_in=200,
                             tokens_out=100, cost_usd=2.5, status="ok"),
                UsageLogRow(id=912, project_id=902, provider="mistral", tokens_in=10,
                             tokens_out=5, cost_usd=0.1, status="ok"),
                UsageLogRow(id=913, project_id=None, provider="cerebras", tokens_in=5,
                             tokens_out=5, cost_usd=0.0, status="ok"),
            ])

    asyncio.get_event_loop().run_until_complete(_seed())
    totals, proj_spend = asyncio.get_event_loop().run_until_complete(
        _fetch_range_and_proj_spend("", {})
    )

    # Grand total covers ALL rows, including the project-less one.
    assert totals["calls"] == 4
    assert totals["spend"] == pytest.approx(4.1)
    assert totals["tin"] == 315
    assert totals["tout"] == 160
    # Per-project dict excludes the project-less row, matches per-project sums.
    assert proj_spend[901] == pytest.approx(4.0)
    assert proj_spend[902] == pytest.approx(0.1)
    assert None not in proj_spend


def test_fetch_range_and_proj_spend_date_range_excludes_out_of_range_rows():
    """Sargable bounds must still filter correctly at the day boundary."""
    import asyncio
    from datetime import date, datetime

    from aibroker.db import get_session
    from aibroker.db.models import UsageLogRow
    from aibroker.routes.dashboard_data import _fetch_range_and_proj_spend, _range_where

    async def _seed():
        async with get_session() as s:
            s.add_all([
                UsageLogRow(id=920, provider="gemini", cost_usd=1.0, status="ok",
                             created_at=datetime(2026, 6, 2, 12, 0)),   # in range
                UsageLogRow(id=921, provider="gemini", cost_usd=5.0, status="ok",
                             created_at=datetime(2026, 6, 1, 23, 59)),  # before
                UsageLogRow(id=922, provider="gemini", cost_usd=7.0, status="ok",
                             created_at=datetime(2026, 6, 3, 0, 0)),    # after (next day start)
            ])

    asyncio.get_event_loop().run_until_complete(_seed())
    where, bind_ = _range_where(date(2026, 6, 2), date(2026, 6, 2))
    totals, _ = asyncio.get_event_loop().run_until_complete(
        _fetch_range_and_proj_spend(where, bind_)
    )
    assert totals["calls"] == 1
    assert totals["spend"] == pytest.approx(1.0)


def test_fetch_tokens_today_aggregates_only_todays_rows():
    """Portable SQL (Python-computed sargable bounds, no ::date cast) — must
    include today's UTC rows and exclude yesterday's."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow, UsageLogRow
    from aibroker.routes.dashboard_data import _fetch_tokens_today

    today = datetime.now(UTC).replace(tzinfo=None)
    yesterday = today - timedelta(days=1)

    async def _seed():
        async with get_session() as s:
            s.add_all([
                # Explicit id: real FK target for usage_log.api_key_id below
                # (Postgres enforces it; SQLite BigInteger PK needs it anyway).
                ApiKeyRow(id=905, provider="gemini", label="k-tokens-today",
                           tier="free", scopes=["llm:chat"], token_encrypted="x",
                           is_active=True, is_alive=True),
                UsageLogRow(id=930, api_key_id=905, provider="gemini", tokens_in=100,
                             tokens_out=50, status="ok", created_at=today),
                UsageLogRow(id=931, api_key_id=905, provider="gemini", tokens_in=10,
                             tokens_out=5, status="ok", created_at=yesterday),
            ])

    asyncio.get_event_loop().run_until_complete(_seed())
    tokens_today = asyncio.get_event_loop().run_until_complete(_fetch_tokens_today())
    assert tokens_today[905] == {"tot": 150, "tin": 100, "tout": 50}


def test_fetch_projects_and_keys_return_seeded_rows():
    """Plain ORM selects — portable, no Postgres-only syntax."""
    import asyncio

    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow, ProjectRow
    from aibroker.routes.dashboard_data import _fetch_keys, _fetch_projects

    async def _seed():
        async with get_session() as s:
            s.add_all([
                ProjectRow(id=906, name="p-gamma", project_key_prefix="aib_prj_g",
                            project_key_hash="hg", allowed_scopes=["llm:chat"],
                            is_active=True, notes=""),
                ApiKeyRow(id=907, provider="gemini", label="k-gamma", tier="free",
                           scopes=["llm:chat"], token_encrypted="x",
                           is_active=True, is_alive=True),
            ])

    asyncio.get_event_loop().run_until_complete(_seed())
    projects = asyncio.get_event_loop().run_until_complete(_fetch_projects())
    keys = asyncio.get_event_loop().run_until_complete(_fetch_keys())
    assert any(p.name == "p-gamma" for p in projects)
    assert any(k.label == "k-gamma" for k in keys)


# ─── Logout ─────────────────────────────────────────────────────────────────


def test_logout_clears_cookie():
    """Logout is POST-only since 2026-10-02 (a GET could be fired cross-site)."""
    r = client.post("/logout", cookies=_logged_in_cookies(), follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers["location"]
    # Cookie cleared via Set-Cookie: ... Max-Age=0
    cookies = r.headers.get("set-cookie", "")
    assert COOKIE_NAME in cookies


def test_logout_get_does_not_log_out():
    r = client.get("/logout", cookies=_logged_in_cookies(), follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert COOKIE_NAME not in r.headers.get("set-cookie", "")


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
    """allowed_scopes is now a checkbox multi-select (list[str]), validated
    against _KNOWN_SCOPES like key scopes — no scopes checked at all."""
    r = client.post(
        "/dashboard/projects/99999/edit",
        cookies=_logged_in_cookies(),
        data={"name": "x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Bad+or+empty+scope" in r.headers["location"]


def test_dashboard_edit_project_rejects_unknown_scope():
    r = client.post(
        "/dashboard/projects/99999/edit",
        cookies=_logged_in_cookies(),
        data={"name": "x", "allowed_scopes": "not-a-real-scope"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Bad+or+empty+scope" in r.headers["location"]


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


def test_parse_date_range_parses_valid_iso():
    from datetime import date

    from aibroker.routes.dashboard_data import _parse_date_range
    df, dt = _parse_date_range("2026-06-01", "2026-06-10")
    assert df == date(2026, 6, 1) and dt == date(2026, 6, 10)


def test_parse_date_range_swaps_inverted_range():
    """If user passes from>to, swap them rather than throw."""
    from datetime import date

    from aibroker.routes.dashboard_data import _parse_date_range
    df, dt = _parse_date_range("2026-06-10", "2026-06-01")
    assert df == date(2026, 6, 1) and dt == date(2026, 6, 10)


def test_parse_date_range_falls_back_on_garbage_when_partial():
    """Garbage on one side becomes today; swap then puts the older one first."""
    from datetime import UTC, date, datetime

    from aibroker.routes.dashboard_data import _parse_date_range
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

    from aibroker.routes.dashboard_data import _parse_date_range
    today = datetime.now(UTC).date()
    # only from → to = today
    df, dt = _parse_date_range("2026-06-01", None)
    assert df == date(2026, 6, 1) and dt == today
    # only to → from = today (and swapped if needed; today > 2026-06-01 so swap happens)
    df, dt = _parse_date_range(None, "2026-06-01")
    assert dt == today


def test_range_hours_table_complete():
    """The 1h/4h/12h/24h/7d/30d range pills must all map to valid hour windows."""
    from aibroker.routes.dashboard_data import _RANGE_HOURS
    assert _RANGE_HOURS["1h"] == 1
    assert _RANGE_HOURS["4h"] == 4
    assert _RANGE_HOURS["12h"] == 12
    assert _RANGE_HOURS["24h"] == 24
    assert _RANGE_HOURS["7d"] == 168
    assert _RANGE_HOURS["30d"] == 720
    # short-first ordering — drives the pill display order on the drill-down page
    assert list(_RANGE_HOURS.keys()) == ["1h", "4h", "12h", "24h", "7d", "30d"]


# ─── Project-detail rendering (unit, no DB) ────────────────────────────────


# ─── Latency histogram ───────────────────────────────────────────────────────


def test_lat_labels_align_with_edges():
    """width_bucket yields len(edges)+1 buckets — labels must match 1:1."""
    from aibroker.routes.dashboard_data import _LAT_EDGES_MS, _LAT_LABELS
    assert len(_LAT_LABELS) == len(_LAT_EDGES_MS) + 1


def test_lat_hist_counts_maps_sparse_to_dense():
    """width_bucket returns only non-empty buckets; missing ones become 0."""
    from collections import namedtuple

    from aibroker.routes.dashboard_data import _LAT_LABELS, _lat_hist_counts
    Row = namedtuple("Row", "b n")
    counts = _lat_hist_counts([Row(0, 5), Row(3, 2), Row(7, 1)])
    assert len(counts) == len(_LAT_LABELS)
    assert counts[0] == 5 and counts[3] == 2 and counts[7] == 1
    assert counts[1] == 0 and counts[6] == 0


# ─── Prompt-cache KPI card ────────────────────────────────────────────────────


# ─── Main dashboard render (unit, no DB) ───────────────────────────────────


# ─── Range-pill active-state indicator ───────────────────────────────────────


# ─── recent-calls log cell humanization (http/kind → friendly label) ─────────


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


def test_dashboard_edit_project_404_when_missing():
    r = client.post(
        "/dashboard/projects/99999/edit",
        cookies=_logged_in_cookies(),
        data={"name": "x", "allowed_scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Project+not+found" in r.headers["location"]


@pytest.mark.skipif(
    ON_SQLITE,
    reason="dashboard queries use Postgres-only now() / FILTER",
)
def test_dashboard_html_still_no_store():
    """The data-bearing HTML document itself must stay no-store even though
    its static assets are now long-cached."""
    r = client.get("/dashboard", cookies=_logged_in_cookies())
    assert "no-store" in r.headers["cache-control"]


# ─── audit rows record the honest client IP (X-Forwarded-For) ────────────────


def test_tg_login_success_audits_xff_client_ip(monkeypatch):
    """Behind CF+nginx request.client.host is the proxy — the login.success
    audit row must carry the FIRST X-Forwarded-For entry instead."""
    import hashlib as _h
    import hmac as _hm
    import time as _t
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr(get_settings(), "TELEGRAM_BOT_TOKEN", "111:test-token")
    owner = get_settings().OWNER_TELEGRAM_ID
    data = {"id": str(owner), "first_name": "D", "auth_date": str(int(_t.time()))}
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = _h.sha256(b"111:test-token").digest()
    data["hash"] = _hm.new(secret, check.encode(), _h.sha256).hexdigest()

    with patch("aibroker.routes.dashboard.audit", AsyncMock()) as fake_audit:
        r = client.get("/api/tg_login", params=data,
                       headers={"X-Forwarded-For": "203.0.113.7, 172.68.1.1"},
                       follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/dashboard"
    assert fake_audit.await_args.kwargs["ip"] == "203.0.113.7"


def test_delete_key_audits_xff_client_ip():
    """key.delete audit ip honours X-Forwarded-For. Explicit id keeps the
    seed SQLite-portable (BigInteger PKs don't autoincrement there)."""
    import asyncio
    from unittest.mock import AsyncMock, patch

    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow

    async def _seed():
        async with get_session() as s:
            s.add(ApiKeyRow(id=930, provider="cerebras", label="del-me",
                            token_encrypted="enc", tier="free",
                            scopes=["llm:chat"], is_active=True, is_alive=True))

    asyncio.get_event_loop().run_until_complete(_seed())
    with patch("aibroker.routes.dashboard.audit", AsyncMock()) as fake_audit:
        r = client.post("/dashboard/keys/930/delete",
                        cookies=_logged_in_cookies(),
                        headers={"X-Forwarded-For": "198.51.100.4, 10.0.0.1"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert fake_audit.await_args.kwargs["ip"] == "198.51.100.4"


def test_scope_checkboxes_disable_what_provider_cannot_serve():
    """A key form must not offer a scope its provider can never serve — the
    box is disabled (so it can't POST) and struck through."""
    from aibroker.routes.dashboard_scopes import _scope_checkboxes
    html = _scope_checkboxes(["llm:chat"], provider="anthropic")
    assert 'value="llm:audio" disabled' in html.replace('" ', '" ')
    assert "llm:audio" in html and "scope-na" in html
    # what it CAN serve stays enabled
    chat = html.split('value="llm:chat"')[1].split("</label>")[0]
    assert "disabled" not in chat


def test_scope_checkboxes_without_provider_disable_nothing():
    """Project forms aren't tied to a provider — every scope stays offerable."""
    from aibroker.routes.dashboard_scopes import _scope_checkboxes
    html = _scope_checkboxes(["llm:chat"], "allowed_scopes")
    assert "disabled" not in html and "scope-na" not in html


# ─── delete project (2026-09-12, owner: the panel had no way to remove a client) ──


def test_dashboard_delete_project_requires_auth():
    r = client.post("/dashboard/projects/42/delete", follow_redirects=False)
    assert r.status_code == 401


def test_dashboard_delete_project_removes_row_and_audits():
    import asyncio
    from unittest.mock import AsyncMock, patch

    from aibroker.db import get_session
    from aibroker.db.models import ProjectRow

    async def _seed():
        async with get_session() as s:
            s.add(ProjectRow(id=931, name="sniffer-old", project_key_hash="h",
                             project_key_prefix="aib_prj_x", allowed_scopes=["llm:chat"]))

    async def _exists():
        async with get_session() as s:
            return await s.get(ProjectRow, 931)

    asyncio.get_event_loop().run_until_complete(_seed())
    with patch("aibroker.routes.dashboard.audit", AsyncMock()) as fake_audit:
        r = client.post("/dashboard/projects/931/delete",
                        cookies=_logged_in_cookies(), follow_redirects=False)
    assert r.status_code == 303 and "deleted" in r.headers["location"]
    assert asyncio.get_event_loop().run_until_complete(_exists()) is None
    assert fake_audit.await_args.kwargs["action"] == "project.delete"
    assert fake_audit.await_args.kwargs["target"] == "sniffer-old"
    # unknown id: honest flash, no crash
    r = client.post("/dashboard/projects/931/delete",
                    cookies=_logged_in_cookies(), follow_redirects=False)
    assert r.status_code == 303 and "not+found" in r.headers["location"]


# ─── logs page: the exact model, routing name in the tooltip (2026-09-13) ────


# ─── 2026-10-02 site review: dashboard hardening ────────────────────────────


def test_scope_checkboxes_escape_the_provider_in_the_tooltip():
    """S16: `provider` is operator/API-supplied; it reached title="..." raw."""
    from aibroker.routes.dashboard_scopes import _scope_checkboxes
    html = _scope_checkboxes(["llm:chat"], provider='"><script>alert(1)</script>')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html and "&quot;" in html


def test_is_known_provider_uses_the_adapter_table():
    from aibroker.providers.registry import default_models
    from aibroker.routes.dashboard_scopes import _is_known_provider
    assert all(_is_known_provider(p) for p in default_models())
    assert not _is_known_provider("evil<script>")


def test_dashboard_create_key_rejects_unknown_provider():
    r = client.post(
        "/dashboard/keys/create", cookies=_logged_in_cookies(),
        data={"provider": '"><script>x', "label": "y", "token": "tok-12345678",
              "scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Unknown+provider" in r.headers["location"]


def test_dashboard_create_key_refuses_provider_in_no_chain():
    """S20: mistral is in no chain (2026-09-12) — such a key can never be
    picked and its scope boxes are all disabled, so refuse adding it."""
    r = client.post(
        "/dashboard/keys/create", cookies=_logged_in_cookies(),
        data={"provider": "mistral", "label": "y", "token": "tok-12345678",
              "scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "no+routing+chain" in r.headers["location"]


@pytest.mark.parametrize("bad", ["abc", "nan", "inf", "-inf", "-1", "1e999"])
def test_parse_cost_cap_rejects_junk_nonfinite_and_negative(bad):
    from aibroker.routes.dashboard import _parse_cost_cap
    with pytest.raises(ValueError):
        _parse_cost_cap(bad)


def test_parse_cost_cap_accepts_blank_zero_and_decimals():
    from aibroker.routes.dashboard import _parse_cost_cap
    assert _parse_cost_cap("") is None
    assert _parse_cost_cap("   ") is None
    assert _parse_cost_cap("0") == 0.0
    assert _parse_cost_cap(" 1.25 ") == 1.25


@pytest.mark.parametrize("path,data", [
    ("/dashboard/keys/99999/edit", {"label": "x", "tier": "free", "scopes": "llm:chat"}),
    ("/dashboard/projects/99999/edit", {"name": "x", "allowed_scopes": "llm:chat"}),
    ("/dashboard/keys/create", {"provider": "cerebras", "label": "x",
                                "token": "tok-12345678", "scopes": "llm:chat"}),
    ("/dashboard/projects/create", {"name": "x", "allowed_scopes": "llm:chat"}),
])
def test_dashboard_forms_flash_on_bad_cost_cap_instead_of_500(path, data):
    for bad in ("abc", "nan"):
        r = client.post(path, cookies=_logged_in_cookies(),
                        data={**data, "daily_cost_cap_usd": bad}, follow_redirects=False)
        assert r.status_code == 303, (path, bad)
        assert "Bad+cost+cap" in r.headers["location"]


def test_flash_url_percent_encodes_names():
    """S18: raw names broke the query string (`&`, `#`, `%`) and could inject params."""
    from aibroker.routes.dashboard import _flash_url
    assert _flash_url("Key a&b#c/d updated") == "/dashboard?flash=Key+a%26b%23c%2Fd+updated"
    assert _flash_url("!Bad") == "/dashboard?flash=%21Bad"


@pytest.mark.parametrize("path,data,msg", [
    ("/dashboard/keys/99999/edit", {"tier": "free", "scopes": "llm:chat"}, "Label+too+long"),
    ("/dashboard/projects/99999/edit", {"allowed_scopes": "llm:chat"}, "Name+too+long"),
    ("/dashboard/projects/create", {"allowed_scopes": "llm:chat"}, "Name+too+long"),
])
def test_dashboard_forms_cap_name_and_label_at_100_chars(path, data, msg):
    field = "label" if "keys" in path else "name"
    r = client.post(path, cookies=_logged_in_cookies(),
                    data={**data, field: "x" * 101}, follow_redirects=False)
    assert r.status_code == 303
    assert msg in r.headers["location"]


def test_dashboard_create_key_caps_label_at_100_chars():
    r = client.post(
        "/dashboard/keys/create", cookies=_logged_in_cookies(),
        data={"provider": "cerebras", "label": "x" * 101, "token": "tok-12345678",
              "scopes": "llm:chat"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Label+too+long" in r.headers["location"]


class _FakeSession:
    """Just enough AsyncSession for the edit-key handler's get()."""
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, key):
        return self.row


def _edit_key_with_row(monkeypatch, row, data):
    import aibroker.routes.dashboard as dash

    async def no_audit(**kw):
        return None
    monkeypatch.setattr(dash, "get_session", lambda: _FakeSession(row))
    monkeypatch.setattr(dash, "audit", no_audit)
    return client.post(f"/dashboard/keys/{row.id}/edit", cookies=_logged_in_cookies(),
                       data=data, follow_redirects=False)


def test_edit_key_of_a_provider_in_no_chain_keeps_its_scopes(monkeypatch):
    """S20: every scope box is disabled for such a key (disabled boxes are not
    submitted), so saving used to fail 'Bad or empty scope' and the key was
    un-editable. It now keeps what it has."""
    from aibroker.db.models import ApiKeyRow
    row = ApiKeyRow(id=5, provider="mistral", label="m", tier="free",
                    scopes=["llm:chat", "llm:edit"], token_encrypted="x",
                    is_active=True, is_alive=True)
    r = _edit_key_with_row(monkeypatch, row, {"label": "m2", "tier": "paid"})
    assert r.status_code == 303
    assert "updated" in r.headers["location"] and "Bad" not in r.headers["location"]
    assert row.scopes == ["llm:chat", "llm:edit"]
    assert row.label == "m2" and row.tier == "paid"


def test_edit_key_of_a_chained_provider_still_requires_a_scope(monkeypatch):
    from aibroker.db.models import ApiKeyRow
    row = ApiKeyRow(id=6, provider="cerebras", label="c", tier="free",
                    scopes=["llm:chat"], token_encrypted="x",
                    is_active=True, is_alive=True)
    r = _edit_key_with_row(monkeypatch, row, {"label": "c", "tier": "free"})
    assert r.status_code == 303
    assert "Bad+or+empty+scope" in r.headers["location"]
    assert row.scopes == ["llm:chat"]



