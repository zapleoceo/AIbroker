"""routes/health — public endpoints."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aibroker.main import app

client = TestClient(app)

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


def test_landing_returns_html():
    """Landing covered in detail in test_routes_landing.py; this is a smoke."""
    r = client.get("/")
    assert r.status_code == 200
    assert "AIbroker" in r.text
    assert "/docs" in r.text
    # New bilingual landing markers
    assert 'data-lang="en"' in r.text
    assert 'data-lang="ru"' in r.text


def test_healthz_returns_json():
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["service"] == "aibroker"
    assert "ts" in data


@pytest.mark.skipif(
    ON_SQLITE,
    reason="/v1/health uses Postgres-only now() and FILTER (...)",
)
def test_v1_health_returns_providers_array():
    """No Accept header (curl/scripts default) — same JSON contract as always."""
    r = client.get("/v1/health")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    data = r.json()
    assert "providers" in data
    assert isinstance(data["providers"], list)


@pytest.mark.skipif(
    ON_SQLITE,
    reason="/v1/health uses Postgres-only now() and FILTER (...)",
)
def test_v1_health_html_for_browser_accept():
    """A browser (Accept: text/html) gets the colored status page, not JSON."""
    r = client.get("/v1/health", headers={
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Provider health" in r.text
    assert "no-store" in r.headers.get("cache-control", "")


# ─── Render helpers (unit — no DB) ─────────────────────────────────────────


def test_health_provider_card_renders_bar_and_counts():
    from aibroker.routes.health import _health_provider_card
    html = _health_provider_card(
        {"provider": "cerebras", "alive": 3, "cooldown": 1, "dead": 0, "total": 4}
    )
    assert "cerebras" in html
    assert "seg-good" in html and "seg-warn" in html
    assert "seg-bad" not in html   # dead=0 → no red segment drawn
    assert "3 <b" in html and "1 <b" in html and "4 <b" in html


def test_health_provider_card_all_dead_shows_only_bad_segment():
    from aibroker.routes.health import _health_provider_card
    html = _health_provider_card(
        {"provider": "openrouter", "alive": 0, "cooldown": 0, "dead": 5, "total": 5}
    )
    assert "seg-bad" in html
    assert "seg-good" not in html and "seg-warn" not in html


def test_health_provider_card_escapes_provider_name():
    """provider is DB-controlled (our own config), but esc() is defense in
    depth — matches the rest of the codebase's convention."""
    from aibroker.routes.health import _health_provider_card
    html = _health_provider_card(
        {"provider": "<script>x</script>", "alive": 0, "cooldown": 0, "dead": 0, "total": 1}
    )
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_health_html_totals_and_bilingual_markers():
    from aibroker.routes.health import _render_health_html
    r = _render_health_html([
        {"provider": "cerebras", "alive": 3, "cooldown": 1, "dead": 0, "total": 4},
        {"provider": "gemini", "alive": 0, "cooldown": 0, "dead": 2, "total": 2},
    ])
    body = r.body.decode()
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "no-store" in r.headers.get("cache-control", "")
    # totals summed across providers (3+0 alive, 1+0 cooldown, 0+2 dead, 4+2 total)
    assert '<div class="n">3</div>' in body   # alive
    assert '<div class="n">1</div>' in body   # cooldown
    assert '<div class="n">2</div>' in body   # dead
    assert '<div class="n">6</div>' in body   # total
    assert 'data-lang="en"' in body and 'data-lang="ru"' in body
    assert body.count('class="pcard"') == 2


def test_render_health_html_no_providers_shows_empty_state():
    from aibroker.routes.health import _render_health_html
    body = _render_health_html([]).body.decode()
    assert "No keys configured yet." in body
    assert 'class="pcard"' not in body


def test_health_code_snippet_survives_lang_toggle():
    """REGRESSION: the machine-readable curl example must NOT sit inside a
    data-i18n element's own content — the lang-toggle JS does
    el.textContent = <data-en|data-ru value>, which would flatten an embedded
    <code> tag into literal visible angle-bracket text on every RU/EN switch."""
    from aibroker.routes.health import _render_health_html
    body = _render_health_html([]).body.decode()
    assert "<code>curl" in body
    # the <code> block must sit OUTSIDE the translated span's own tag
    i18n_start = body.index("Machine-readable form")
    span_end = body.index("</span>", i18n_start)
    assert "<code>" not in body[i18n_start:span_end]


def test_openapi_docs_served():
    r = client.get("/docs")
    assert r.status_code == 200
    assert "swagger" in r.text.lower() or "openapi" in r.text.lower()


def test_openapi_json_served():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "paths" in spec
    assert "/healthz" in spec["paths"]
    assert "/v1/health" in spec["paths"]
    assert "/v1/chat" in spec["paths"]


def test_unknown_path_returns_404():
    r = client.get("/this/path/does/not/exist")
    assert r.status_code == 404
