"""routes/landing — bilingual EN/RU public landing page."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aibroker.main import app


client = TestClient(app)


def test_landing_returns_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_landing_contains_brand():
    r = client.get("/")
    assert "AIbroker" in r.text


def test_landing_has_both_languages_embedded():
    """Both EN and RU strings are present so JS can swap them."""
    r = client.get("/")
    # EN
    assert "How it works" in r.text
    assert "Free first" in r.text
    assert "Get started" in r.text
    # RU
    assert "Как работает" in r.text
    assert "Бесплатные — первыми" in r.text
    assert "Начать" in r.text


def test_landing_has_lang_toggle():
    r = client.get("/")
    assert 'data-lang="en"' in r.text
    assert 'data-lang="ru"' in r.text
    assert "localStorage" in r.text


def test_landing_default_lang_is_english():
    """First-paint HTML has lang='en'."""
    r = client.get("/")
    assert '<html lang="en"' in r.text


def test_landing_has_all_sections():
    r = client.get("/")
    # Nav links use #anchor refs; FAQ is rendered but not in nav
    for anchor in ["#how", "#features", "#providers", "#api", "#pricing"]:
        assert anchor in r.text, f"Missing nav anchor {anchor}"
    for sec_id in ['id="problem"', 'id="how"', 'id="features"',
                    'id="providers"', 'id="api"', 'id="pricing"', 'id="faq"']:
        assert sec_id in r.text, f"Missing section {sec_id}"


def test_landing_links_to_docs():
    r = client.get("/")
    assert "/docs" in r.text
    assert "/openapi.json" in r.text


def test_landing_links_to_dashboard_and_login():
    r = client.get("/")
    assert "/dashboard" in r.text
    assert "/login" in r.text


def test_landing_lists_providers():
    r = client.get("/")
    for p in ["cerebras", "groq", "gemini", "mistral", "cohere",
               "openrouter", "voyage", "deepseek", "anthropic", "openai"]:
        assert p in r.text


def test_landing_shows_version():
    """{version} placeholder is interpolated."""
    from aibroker import __version__
    r = client.get("/")
    assert f"v{__version__}" in r.text
