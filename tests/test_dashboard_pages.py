"""Admin UI pages: every page renders (empty + seeded), key elements present,
auth gates, HTMX fragments, CSV export, static assets."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aibroker.main import app
from tests import dash_seed as ds

client = TestClient(app)
ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")

PAGES = [
    "/dashboard", "/dashboard/requests", "/dashboard/projects", "/dashboard/providers",
    "/dashboard/audit", "/dashboard/settings",
]


def _get(path: str, **kw):
    return ds.get(client, path, **kw)


# ─── auth ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", PAGES + [
    "/dashboard/requests.csv", "/dashboard/requests/1", "/dashboard/projects/1",
    "/dashboard/projects/new", "/dashboard/keys/new", "/dashboard/keys/1/edit"])
def test_every_page_requires_login(path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_htmx_fragment_without_session_gets_hx_redirect_not_a_login_page():
    r = client.get("/dashboard/requests/1", headers={"HX-Request": "true"},
                   follow_redirects=False)
    assert r.status_code == 204 and r.headers["HX-Redirect"] == "/login"


def test_admin_key_header_also_opens_pages():
    from aibroker.config import get_settings
    r = client.get("/dashboard/settings", headers={"X-Admin-Key": get_settings().ADMIN_KEY})
    assert r.status_code == 200


# ─── empty states ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,marker", [
    ("/dashboard", "No calls in this period"),
    ("/dashboard/requests", "No requests match"),
    ("/dashboard/projects", "No projects yet"),
    ("/dashboard/providers", "No API keys yet"),
    ("/dashboard/audit", "No audit entries"),
])
def test_empty_database_renders_an_empty_state(path, marker):
    r = _get(path)
    assert r.status_code == 200
    assert marker in r.text


def test_empty_overview_still_has_kpis_and_all_clear():
    body = _get("/dashboard").text
    for label in ("Spend", "Calls", "Success rate", "Cache hit", "p95 latency", "Active keys"):
        assert label in body
    assert "All clear" in body


def test_models_and_settings_render_without_data():
    assert "deepseek/deepseek-flash" in _get("/dashboard/providers").text
    assert "v" in _get("/dashboard/settings").text


# ─── shell: semantics, a11y, no third-party at runtime ──────────────────────


@pytest.mark.parametrize("path", PAGES)
def test_shell_is_semantic_accessible_and_self_hosted(path):
    body = _get(path).text
    assert '<main id="main"' in body and 'class="skip"' in body
    assert 'aria-current="page"' in body                       # active nav item
    assert '<dialog id="drawer"' in body and 'aria-labelledby="drawer-title"' in body
    assert '<dialog id="confirm"' in body
    assert 'name="viewport"' in body
    for cdn in ("cdn.jsdelivr", "unpkg.com", "cdnjs", "googleapis", "telegram.org"):
        assert cdn not in body, f"{cdn} leaked into {path}"
    for lib in ("htmx.min.js", "alpine.min.js", "uPlot.iife.min.js", "app.css", "tokens.css"):
        assert "/dashboard/static/" in body and lib in body


def test_pages_are_no_store_and_bilingual():
    r = _get("/dashboard")
    assert "no-store" in r.headers["cache-control"]
    assert 'data-ru="' in r.text and 'data-lang="ru"' in r.text


def test_bottom_tab_bar_and_rail_both_present():
    body = _get("/dashboard").text
    assert 'class="rail"' in body and 'class="tabbar"' in body
    for href in ("/dashboard/requests", "/dashboard/projects", "/dashboard/providers",
                 "/dashboard/audit", "/dashboard/settings"):
        assert f'href="{href}"' in body
    assert 'href="/dashboard/keys"' not in body and 'href="/dashboard/models"' not in body
    assert 'href="/dashboard/jobs"' not in body              # merged into Requests


def test_range_picker_presets_and_custom_form():
    body = _get("/dashboard", params={"range": "30d"}).text
    for key in ("today", "7d", "30d", "all"):
        assert f"range={key}" in body
    assert 'name="from"' in body and 'name="to"' in body
    assert body.count('aria-current="true"') == 1            # exactly one active preset


def test_logout_is_a_post_form_in_the_shell():
    body = _get("/dashboard").text
    assert 'method="post" action="/logout"' in body


# ─── overview (seeded) ──────────────────────────────────────────────────────


def test_overview_with_data_shows_chart_attention_and_provider_health():
    ds.seed_demo()
    body = _get("/dashboard", params={"range": "today"}).text
    assert 'id="chart-usage-data"' in body and 'data-chart="stacked"' in body
    assert "dead key" in body and "alive-key" not in body.split("Needs attention")[1][:400]
    assert 'href="/dashboard/providers#p-gemini"' in body
    assert "Provider health" in body and ">gemini<" in body
    assert "Top projects" in body and "stepan" in body
    assert "Job queue" in body


def test_overview_flash_is_shown_and_errors_are_styled():
    assert 'class="flash"' in _get("/dashboard", params={"flash": "Key x added"}).text
    assert 'class="flash err"' in _get("/dashboard", params={"flash": "!Bad cap"}).text


# ─── projects ───────────────────────────────────────────────────────────────


def test_projects_cards_show_spend_vs_cap_and_cache_badge():
    ds.seed_demo()
    body = _get("/dashboard/projects", params={"range": "all"}).text
    assert "stepan" in body and "vera" in body
    assert "Today vs daily cap" in body and 'role="progressbar"' in body
    assert "cache" in body and 'class="spark' in body
    assert 'hx-get="/dashboard/projects/new"' in body


def test_project_new_form_is_a_labelled_drawer_form():
    html = _get("/dashboard/projects/new", headers={"HX-Request": "true"}).text
    assert 'action="/dashboard/projects/create"' in html
    assert 'name="name"' in html and 'name="allowed_scopes"' in html
    assert 'name="daily_cost_cap_usd"' in html and 'name="next"' in html
    assert 'for="pf-name"' in html


@pytest.mark.parametrize("tab,marker", [
    ("usage", "By provider"), ("models", "Top models"),
    ("keys", "Keys that served this project"), ("settings", "Danger zone")])
def test_project_detail_tabs_render(tab, marker):
    ds.seed_demo()
    r = _get("/dashboard/projects/1", params={"tab": tab, "range": "all"})
    assert r.status_code == 200 and marker in r.text
    assert 'aria-label="Project sections"' in r.text


def test_project_detail_usage_has_kpis_breakdowns_and_recent_calls():
    ds.seed_demo()
    body = _get("/dashboard/projects/1", params={"range": "all"}).text
    for marker in ("Tokens in / out", "Latency distribution", "By capability", "By workflow",
                   "Recent 50 calls", "Rotate token", "Today vs daily cap"):
        assert marker in body
    assert "Cache hit" in body                                 # seeded cache_read rows


def test_project_detail_settings_has_edit_form_rotate_and_confirmed_delete():
    ds.seed_demo()
    body = _get("/dashboard/projects/1", params={"tab": "settings"}).text
    assert 'action="/dashboard/projects/1/edit"' in body
    assert 'action="/dashboard/projects/1/rotate-token"' in body
    assert 'action="/dashboard/projects/1/delete"' in body
    assert 'data-confirm="Delete project stepan?' in body


def test_project_detail_unknown_project_redirects_with_flash():
    r = _get("/dashboard/projects/4242")
    assert r.status_code == 303 and "Project+not+found" in r.headers["location"]


def _once_cookie(resp):
    from aibroker.routes.dashboard_pages import ONCE_COOKIE
    cookie = resp.cookies.get(ONCE_COOKIE)
    assert cookie, "one-time cookie not set"
    return cookie


@pytest.mark.skipif(ON_SQLITE, reason="BIGINT PK cannot autoincrement on SQLite")
def test_create_project_redirects_then_shows_the_new_key_once():
    r = client.post("/dashboard/projects/create", cookies=ds.cookies(), follow_redirects=False,
                    data={"name": "fresh", "allowed_scopes": ["llm:chat"],
                          "daily_cost_cap_usd": "0.20"})
    assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/projects")
    page = client.get("/dashboard/projects", cookies={**ds.cookies(), "aib_once": _once_cookie(r)})
    assert "aib_prj_" in page.text and "cannot be shown again" in page.text
    assert "aib_once=" in page.headers.get("set-cookie", "")        # cleared after one view
    assert "cannot be shown again" not in _get("/dashboard/projects").text


def test_create_project_requires_an_explicit_cost_cap():
    r = client.post("/dashboard/projects/create", cookies=ds.cookies(), follow_redirects=False,
                    data={"name": "nocap", "allowed_scopes": ["llm:chat"]})
    assert r.status_code == 303 and "required" in r.headers["location"]
    r = client.post("/dashboard/projects/create", cookies=ds.cookies(), follow_redirects=False,
                    data={"name": "nocap", "allowed_scopes": ["llm:chat"],
                          "daily_cost_cap_usd": "-1"})
    assert "Bad+cost+cap" in r.headers["location"]


def test_new_project_form_prefills_a_sane_cap_and_requires_it():
    html = _get("/dashboard/projects/new", headers={"HX-Request": "true"}).text
    assert 'name="daily_cost_cap_usd"' in html and 'value="0.20"' in html and "required" in html
    assert "0 = free providers only" in html


def test_rotate_token_redirects_and_shows_the_new_key_once_so_refresh_is_safe():
    ds.seed(ds.project(1, "stepan"))
    r = client.post("/dashboard/projects/1/rotate-token", cookies=ds.cookies(),
                    data={"next": "/dashboard/projects/1?tab=settings"}, follow_redirects=False)
    assert r.status_code == 303 and "/dashboard/projects/1" in r.headers["location"]
    assert "aib_prj_" not in r.text                                  # never in the POST body
    from sqlalchemy import select

    from aibroker.auth import hash_project_key
    from aibroker.db import get_session
    from aibroker.db.models import ProjectRow

    async def _row():
        async with get_session() as s:
            return (await s.execute(select(ProjectRow).where(ProjectRow.id == 1))).scalar_one()
    row = ds.run(_row())
    assert row.project_key_hash != "hash1" and len(row.project_key_hash) == 64
    page = client.get("/dashboard/projects/1?tab=settings",
                      cookies={**ds.cookies(), "aib_once": _once_cookie(r)})
    assert "save it now" in page.text and "Copy token" in page.text
    new_key = page.text.split("<code>")[1].split("</code>")[0]
    assert hash_project_key(new_key) == row.project_key_hash
    assert "aib_once=" in page.headers.get("set-cookie", "")
    assert "save it now" not in _get("/dashboard/projects/1?tab=settings").text


def test_rotate_token_requires_auth_and_404s_politely():
    assert client.post("/dashboard/projects/1/rotate-token", follow_redirects=False).status_code == 401
    r = client.post("/dashboard/projects/999/rotate-token", cookies=ds.cookies(),
                    follow_redirects=False)
    assert r.status_code == 303 and "Project+not+found" in r.headers["location"]


# ─── keys ───────────────────────────────────────────────────────────────────


def test_providers_page_shows_a_card_per_provider_with_key_rows_and_actions():
    ds.seed_demo()
    body = _get("/dashboard/providers").text
    for provider in ("gemini", "groq", "deepseek", "cerebras"):
        assert f'id="p-{provider}"' in body
    assert "auth failed" in body                               # friendly last_error
    assert 'x-data="countdown(' in body                        # cooldown countdown
    assert ">disabled<" in body and "day cap" in body
    for action in ("/test", "/edit", "/disable", "/delete"):
        assert f"/dashboard/keys/1{action}" in body
    assert 'data-confirm="Delete gemini/alive-key?' in body
    assert 'hx-get="/dashboard/keys/new"' in body
    assert body.count('name="next" value="/dashboard/providers"') >= 2   # actions land back here


def test_providers_page_groups_are_not_swallowed_by_the_dict_keys_method():
    ds.seed(ds.key(1, "gemini", "only"))
    assert ">only<" in _get("/dashboard/providers").text.replace('title="only"', "")


def test_add_key_drawer_collapses_advanced_quota_and_hides_unrouted_providers():
    html = _get("/dashboard/keys/new", headers={"HX-Request": "true"}).text
    assert 'action="/dashboard/keys/create"' in html
    assert '<details class="adv" >' in html or '<details class="adv">' in html
    assert "manual_tok_in_limit" in html and "manual_req_limit" in html
    assert '<option value="mistral">' not in html              # in no routing chain
    assert '<option value="deepseek">' in html
    assert 'x-data="keyForm()"' in html and 'type="password"' in html


def test_edit_key_drawer_prefills_and_opens_advanced_when_limits_set():
    ds.seed(ds.key(1, "gemini", "corp", manual_tok_in_limit=3_000_000, daily_cost_cap_usd=2.5))
    html = _get("/dashboard/keys/1/edit", headers={"HX-Request": "true"}).text
    assert 'action="/dashboard/keys/1/edit"' in html and 'value="corp"' in html
    assert 'value="3000000"' in html and 'value="2.5"' in html
    assert '<details class="adv" open>' in html
    assert "blank = keep the current one" in html


def test_edit_key_drawer_404s_for_unknown_key():
    assert _get("/dashboard/keys/999/edit", headers={"HX-Request": "true"}).status_code == 404


def test_key_test_endpoint_returns_a_status_chip(monkeypatch):
    import aibroker.routes.dashboard as dash
    from aibroker.crypto import encrypt
    ds.seed(ds.key(1, "gemini", "k", token_encrypted=encrypt("tok")))
    seen = {}

    async def fake_probe(provider, token, account_id=None, billable=False):
        seen["args"] = (provider, token)
        return "cooldown", 429, "rate limit"

    async def no_audit(**kw):
        seen["audit"] = kw
    monkeypatch.setattr(dash, "probe", fake_probe)
    monkeypatch.setattr(dash, "audit", no_audit)
    r = client.post("/dashboard/keys/1/test", cookies=ds.cookies())
    assert r.status_code == 200 and "rate limited" in r.text and 'class="chip warn"' in r.text
    assert seen["args"] == ("gemini", "tok") and seen["audit"]["action"] == "key.test"


def test_key_test_endpoint_auth_missing_key_and_bad_token(monkeypatch):
    assert client.post("/dashboard/keys/1/test").status_code == 401
    assert client.post("/dashboard/keys/77/test", cookies=ds.cookies()).status_code == 404
    ds.seed(ds.key(1, "gemini", "k", token_encrypted="not-a-fernet-token"))
    r = client.post("/dashboard/keys/1/test", cookies=ds.cookies())
    assert "decrypt failed" in r.text


def test_disable_key_returns_to_the_page_it_came_from_and_rejects_evil_next():
    ds.seed(ds.key(1, "gemini", "k"))
    r = client.post("/dashboard/keys/1/disable", cookies=ds.cookies(),
                    data={"next": "/dashboard/providers"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/dashboard/providers?flash=")
    r = client.post("/dashboard/keys/1/disable", cookies=ds.cookies(),
                    data={"next": "https://evil.example/x"}, follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard?flash=")


# ─── models / audit / settings ───────────────────────────────────────


def test_providers_models_tab_has_prices_copy_buttons_and_flags_unrouted():
    ds.seed(ds.usage(1, provider="deepseek", model="deepseek/deepseek-flash",
                     capability="chat:smart", latency_ms=900, minutes_ago=60))
    body = _get("/dashboard/providers").text
    assert "deepseek/deepseek-flash" in body and 'data-copy="deepseek/deepseek-flash"' in body
    assert "$0.15 / $0.60" in body                              # per 1M, from the litellm map
    assert "unrouted" in body and "mistral/mistral-small-latest" in body
    assert "900 ms" in body                                     # observed p50
    assert "x-data='providersPage(" in body and 'type="search"' in body


def test_audit_page_filters_and_paginates():
    ds.seed_demo()
    body = _get("/dashboard/audit").text
    assert "key.added" in body and "login.success" in body and "metadata" in body
    only = _get("/dashboard/audit", params={"action": "key."}).text
    assert "key.added" in only and "login.success" not in only
    actor = _get("/dashboard/audit", params={"actor": "tg:1"}).text
    assert "login.success" in actor and "key.added" not in actor
    ds.seed(*[ds.audit_row(100 + i) for i in range(55)])
    paged = _get("/dashboard/audit").text
    assert "Older entries" in paged and "before=" in paged


def test_settings_links_docs_health_and_language():
    body = _get("/dashboard/settings").text
    for marker in ('href="/docs"', 'href="/v1/health"', "Log out", "Audit log", "English"):
        assert marker in body


# ─── static assets ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("path,ctype", [
    ("css/app.css", "text/css"), ("css/tokens.css", "text/css"), ("css/public.css", "text/css"),
    ("js/app.js", "javascript"), ("js/charts.js", "javascript"),
    ("vendor/htmx.min.js", "javascript"), ("vendor/alpine.min.js", "javascript"),
    ("vendor/uPlot.iife.min.js", "javascript"), ("vendor/uPlot.min.css", "text/css")])
def test_static_files_are_served_long_cached_without_auth(path, ctype):
    r = client.get(f"/dashboard/static/{path}")
    assert r.status_code == 200 and ctype in r.headers["content-type"]
    assert "immutable" in r.headers["cache-control"] and len(r.content) > 100


@pytest.mark.parametrize("path", ["../../config.py", "..%2F..%2Fconfig.py", "css", "nope.js",
                                  "css/../../../auth.py"])
def test_static_route_refuses_traversal_and_directories(path):
    assert client.get(f"/dashboard/static/{path}").status_code == 404


def test_asset_urls_are_cache_busted_by_content_hash():
    from aibroker.routes.dashboard_assets import ASSETS_VERSION
    assert len(ASSETS_VERSION) == 12
    assert f"app.css?v={ASSETS_VERSION}" in _get("/dashboard/settings").text


def test_stylesheet_uses_tokens_dark_scheme_and_responsive_rules():
    tokens = client.get("/dashboard/static/css/tokens.css").text
    css = client.get("/dashboard/static/css/app.css").text
    assert "prefers-color-scheme: dark" in tokens and "--accent" in tokens and "--c7" in tokens
    assert "@media (max-width: 860px)" in css and "@media (max-width: 720px)" in css
    assert ":focus-visible" in css and "prefers-reduced-motion" in css
    import re
    stray = re.findall(r"#[0-9a-fA-F]{3,6}\b", css)
    assert not stray, f"hard-coded colours outside tokens.css: {stray[:5]}"


def test_no_hidden_data_sort_hacks_remain():
    for path in PAGES:
        assert "data-sort" not in _get(path).text


# ─── review fixes: i18n, plurals, a11y, flash, layout hooks ─────────────────


def test_plural_helpers_en_and_ru():
    from aibroker.web.format import plural_en, plural_ru
    assert plural_en(1, "error", "errors") == "1 error"
    assert plural_en(2, "error", "errors") == "2 errors"
    forms = ("ошибка", "ошибки", "ошибок")
    got = [plural_ru(n, *forms) for n in (1, 2, 5, 11, 12, 21, 22, 25)]
    assert got == ["1 ошибка", "2 ошибки", "5 ошибок", "11 ошибок", "12 ошибок",
                   "21 ошибка", "22 ошибки", "25 ошибок"]


def test_providers_page_counts_use_plurals_and_translated_labels():
    ds.seed_demo()
    body = _get("/dashboard/providers").text
    assert 'data-en="1 dead" data-ru="1 мёртвый"' in body
    assert 'data-en="1 of 2 keys alive" data-ru="Живых ключей: 1 из 2"' in body
    assert 'data-ru="$ сегодня"' in body
    assert 'data-ru="ошибка авторизации"' in body                 # was raw "auth failed"


def test_quota_labels_and_sources_are_translated():
    ds.seed(ds.key(1, "cerebras", "c", daily_used=10), ds.key(2, "gemini", "g",
            manual_tok_in_limit=1000))
    body = _get("/dashboard/providers").text
    assert 'data-ru="токенов/день"' in body
    assert 'data-ru="лимиты: вручную"' in body
    assert 'data-ru="лимиты: оценка по умолчанию"' in body


def test_attention_plurals_in_both_languages():
    from types import SimpleNamespace as NS

    from aibroker.routes import dashboard_views as v
    rows = [{"key": NS(provider="g", label=str(i)), "status": NS(code="dead"),
             "top_pct": None, "axes": []} for i in range(2)]
    item = v.build_attention(rows, [], [], None)[0]
    assert "2 dead keys" in item["en"] and "2 мёртвых ключа" in item["ru"]
    one = v.build_attention(rows[:1], [], [], None)[0]
    assert "1 dead key " in one["en"] and "1 мёртвый ключ" in one["ru"]


def _icon_only_buttons(html: str):
    import re
    for m in re.finditer(r"<button\b([^>]*)>(.*?)</button>", html, re.S):
        attrs, inner = m.group(1), m.group(2)
        text = re.sub(r"<svg.*?</svg>", "", inner, flags=re.S)
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text and "aria-label" not in attrs:
            yield attrs


@pytest.mark.parametrize("path", PAGES + ["/dashboard/keys/new", "/dashboard/projects/new",
                                          "/dashboard/requests/12", "/dashboard/requests/job-1",
                                          "/dashboard/keys/1/edit"])
def test_icon_only_buttons_have_an_accessible_name(path):
    ds.seed_demo()
    html = _get(path, headers={"HX-Request": "true"}).text
    assert not list(_icon_only_buttons(html))


def test_more_sheet_has_the_same_links_as_the_rail():
    sheet = _get("/dashboard").text.split('class="sheet"')[1]
    for href in ('href="/docs"', 'href="/v1/health"', 'href="/dashboard/settings"'):
        assert href in sheet


def test_disable_flash_names_provider_and_label():
    ds.seed(ds.key(1, "cloudflare", "cf1"))
    r = client.post("/dashboard/keys/1/disable", cookies=ds.cookies(), follow_redirects=False)
    assert "Key+cloudflare%2Fcf1+disabled" in r.headers["location"]


def test_text_is_not_ellipsized_on_phones_and_wide_blocks_can_shrink():
    css = client.get("/dashboard/static/css/app.css").text
    assert ".kpi-sub, .prov small { white-space: normal" in css
    assert ".chip { white-space: normal" in css and ".rowline" in css
    pub = client.get("/dashboard/static/css/public.css").text
    assert ".modes > *" in pub and "min-width: 0" in pub and "max-width: 900px" in pub


def test_providers_rows_have_no_forced_wide_columns():
    body = _get("/dashboard/providers").text
    assert "<table" not in body and 'class="mrow"' in body and 'class="model-id"' in body
