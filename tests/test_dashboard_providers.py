"""The merged Providers page (Keys + Models): view-model, redirects, filters,
key actions and per-model cooldowns."""
from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from aibroker.main import app
from aibroker.routes import dashboard_queries as q
from aibroker.routes import dashboard_views as v
from tests import dash_seed as ds

client = TestClient(app)


def _get(path: str, **kw):
    return ds.get(client, path, **kw)


def _group(provider, codes):
    rows = [{"key": SimpleNamespace(provider=provider, label=str(i), id=i),
             "status": SimpleNamespace(code=c), "axes": [], "cooling_models": []}
            for i, c in enumerate(codes)]
    return v.group_by_provider(rows)[0]


# ─── view-model ─────────────────────────────────────────────────────────────


def test_capability_filters_are_derived_from_the_registry():
    keys = [f["key"] for f in v.capability_filters()]
    assert keys[:6] == ["chat", "structured", "vision", "voice", "embedding", "decision"]
    assert v.cap_group("chat:fast") == "chat" and v.cap_group("transcription") == "voice"
    assert v.cap_group("prefilter") == "chat" and v.cap_group("weird") == "weird"


def test_cards_put_live_providers_first_then_rank_and_fold_inactive():
    cards = v.build_providers([_group("openai", ["alive"]), _group("groq", ["dead"])], {})
    names = [c["provider"] for c in cards]
    assert names[0] == "openai" and names.index("groq") < names.index("gemini")   # live, then rank
    by = {c["provider"]: c for c in cards}
    assert by["mistral"]["inactive"] and not by["gemini"]["inactive"]            # routed, no keys
    assert names.index("mistral") > names.index("deepseek")                      # inactive last
    assert by["groq"]["live"] is False and by["openai"]["paid"] and not by["groq"]["paid"]


def test_card_models_carry_groups_price_observed_and_hints():
    obs = {("deepseek", "deepseek/deepseek-flash"): {"calls": 4, "success": 75.0, "p50": 900}}
    card = next(c for c in v.build_providers([], obs) if c["provider"] == "deepseek")
    mo = next(m for m in card["models"] if m["id"] == "deepseek/deepseek-flash")
    assert mo["p50"] == 900 and round(mo["price_in"], 2) == 0.15 and "chat" in mo["groups"]
    mistral = next(c for c in v.build_providers([], {}) if c["provider"] == "mistral")
    assert mistral["models"] and not any(m["routed"] for m in mistral["models"])


def test_quota_burn_aggregates_enabled_keys_and_picks_the_hottest_axis():
    def row(code, used, cap):
        return {"status": SimpleNamespace(code=code),
                "axes": [{"name": "requests", "label": ("r", "р"), "used": used, "cap": cap}]}
    burn = v._quota_burn([row("alive", 30, 100), row("alive", 50, 100), row("disabled", 99, 100)])
    assert burn["used"] == 80 and burn["cap"] == 200 and burn["pct"] == 40
    assert v._quota_burn([]) is None


def test_model_cooldowns_query_keeps_only_running_ones():
    ds.seed(ds.key(1, "gemini", "k"), ds.model_cooldown(1, "gemini/gemini-3.5-flash", 30),
            ds.model_cooldown(1, "gemini/old", -5))
    cd = ds.run(q.model_cooldowns(ds.now()))
    assert [c["model"] for c in cd[1]] == ["gemini/gemini-3.5-flash"]


# ─── page ───────────────────────────────────────────────────────────────────


def test_page_renders_toolbar_chips_cards_and_tabs():
    ds.seed_demo()
    body = _get("/dashboard/providers").text
    assert 'role="search"' in body and 'id="pf-q"' in body and 'id="pf-live"' in body
    for cap in ("Chat", "Structured", "Vision", "Voice", "Embeddings", "Decisions"):
        assert f'data-en="{cap}"' in body
    for provider in ("gemini", "groq", "deepseek", "cerebras", "mistral"):
        assert f'id="p-{provider}"' in body
    assert 'role="tablist"' in body and body.count('role="tabpanel"') >= 2 * 14
    assert "Inactive providers" in body and 'id="inactive-body"' in body
    assert 'hx-get="/dashboard/keys/new"' in body
    assert "provCard('gemini'" in body and "provCard('mistral', false" in body


def test_filters_seed_from_the_query_string_and_rows_carry_filter_data():
    body = _get("/dashboard/providers", params={"q": "gpt-oss", "cap": "vision", "live": "1"}).text
    assert '"q": "gpt-oss"' in body and '"cap": "vision"' in body and '"live": true' in body
    assert 'data-caps="vision"' in body and 'data-model="groq/openai/gpt-oss-120b"' in body
    bad = _get("/dashboard/providers", params={"cap": "bogus"}).text
    assert '"cap": ""' in bad and '"live": false' in bad


def test_live_flag_marks_cards_with_a_live_key():
    ds.seed(ds.key(1, "gemini", "ok"), ds.key(2, "groq", "dead", is_alive=False))
    body = _get("/dashboard/providers").text
    assert 'data-prov="gemini" data-live="1"' in body and 'data-prov="groq" data-live="0"' in body


def test_per_model_cooldown_is_shown_on_the_key_and_the_model():
    ds.seed(ds.key(1, "gemini", "k"), ds.model_cooldown(1, "gemini/gemini-3.5-flash", 30))
    body = _get("/dashboard/providers").text
    assert 'data-en="model cooling"' in body and "<code>gemini/gemini-3.5-flash</code>" in body
    assert 'x-data="countdown(' in body
    assert 'data-en="1 key cooling"' in body and 'data-ru="1 ключ на паузе"' in body


def test_i18n_strings_are_present_in_both_languages():
    ds.seed(ds.key(1, "gemini", "k"))
    body = _get("/dashboard/providers").text
    for en, ru in (("Only with live keys", "Только с живыми ключами"), ("Models", "Модели"),
                   ("Keys", "Ключи"), ("Inactive providers", "Неактивные провайдеры"),
                   ("free", "бесплатный"), ("Nothing matches", "Ничего не найдено"),
                   ("Add key", "Добавить ключ")):
        assert f'data-en="{en}" data-ru="{ru}"' in body
    assert 'data-en-placeholder="model id or provider"' in body


def test_key_actions_work_from_the_providers_page():
    ds.seed(ds.key(1, "gemini", "k"))
    r = client.post("/dashboard/keys/1/disable", cookies=ds.cookies(),
                    data={"next": "/dashboard/providers"}, follow_redirects=False)
    assert r.headers["location"].startswith("/dashboard/providers?flash=Key+gemini%2Fk+disabled")
    assert 'data-en="Enable"' in _get("/dashboard/providers").text
    r = client.post("/dashboard/keys/1/delete", cookies=ds.cookies(),
                    data={"next": "/dashboard/providers"}, follow_redirects=False)
    assert "Key+gemini%2Fk+deleted" in r.headers["location"]
    assert "No API keys yet" in _get("/dashboard/providers").text


# ─── redirects ──────────────────────────────────────────────────────────────


def test_old_keys_and_models_urls_redirect_to_providers_with_params():
    r = client.get("/dashboard/keys", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/dashboard/providers"
    r = client.get("/dashboard/keys?flash=Key+x+disabled", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/dashboard/providers?flash=Key+x+disabled"
    r = client.get("/dashboard/models", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/dashboard/providers"
    r = client.get("/dashboard/models?provider=groq&cap=vision", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/dashboard/providers?cap=vision&q=groq"


def test_providers_requires_login_and_overview_links_to_it():
    assert client.get("/dashboard/providers", follow_redirects=False).headers["location"] == "/login"
    ds.seed(ds.key(1, "gemini", "k"), ds.usage(1, minutes_ago=3))
    body = _get("/dashboard").text
    assert 'href="/dashboard/providers#p-gemini"' in body and "/dashboard/keys" not in body
    assert 'href="/dashboard/models"' not in body
