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


def test_landing_has_github_link_in_header():
    """Octocat icon in nav-right links to the repo."""
    r = client.get("/")
    assert 'class="gh-link"' in r.text
    assert 'href="https://github.com/zapleoceo/AIbroker"' in r.text
    assert 'aria-label="GitHub repository"' in r.text


def test_landing_hero_has_three_ctas_including_github():
    """Hero shows Get started + API reference + Star on GitHub."""
    r = client.get("/")
    assert 'data-i18n="hero.cta1"' in r.text   # Get started
    assert 'data-i18n="hero.cta2"' in r.text   # API reference
    assert 'data-i18n="hero.cta3"' in r.text   # ★ Star on GitHub
    assert "★ Star on GitHub" in r.text
    assert "Star на GitHub" in r.text


def test_landing_has_open_graph_tags():
    r = client.get("/")
    assert 'property="og:type"' in r.text
    assert 'property="og:url"' in r.text and "aib.zapleo.com" in r.text
    assert 'property="og:title"' in r.text
    assert 'property="og:description"' in r.text
    assert 'property="og:site_name" content="AIbroker"' in r.text


def test_landing_has_twitter_card():
    r = client.get("/")
    assert 'name="twitter:card"' in r.text
    assert 'name="twitter:title"' in r.text


def test_landing_has_schema_org_jsonld():
    """Structured data for Google rich-results + LLM crawlers."""
    import json
    import re
    r = client.get("/")
    m = re.search(
        r'<script type="application/ld\+json">\s*(\{.+?\})\s*</script>',
        r.text, re.DOTALL,
    )
    assert m, "JSON-LD block missing"
    data = json.loads(m.group(1))
    assert data["@context"] == "https://schema.org"
    graph = data["@graph"]
    types = {item["@type"] for item in graph}
    assert {"SoftwareApplication", "FAQPage"} <= types
    sw = next(i for i in graph if i["@type"] == "SoftwareApplication")
    assert sw["name"] == "AIbroker"
    assert sw["codeRepository"].startswith("https://github.com/")
    assert sw["offers"]["price"] == "0"
    # FAQ has the 4 questions
    faq = next(i for i in graph if i["@type"] == "FAQPage")
    assert len(faq["mainEntity"]) == 4


def test_landing_has_canonical_and_hreflang():
    r = client.get("/")
    assert '<link rel="canonical" href="https://aib.zapleo.com/">' in r.text
    assert 'hreflang="en"' in r.text and 'hreflang="ru"' in r.text
    assert 'hreflang="x-default"' in r.text


def test_landing_has_robots_meta_index():
    r = client.get("/")
    assert 'name="robots" content="index, follow"' in r.text


def test_robots_txt_served():
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "User-agent: *" in r.text
    assert "Allow: /" in r.text
    # Sensitive paths blocked
    assert "Disallow: /admin/" in r.text
    assert "Disallow: /dashboard" in r.text
    # Sitemap link
    assert "Sitemap: https://aib.zapleo.com/sitemap.xml" in r.text


def test_sitemap_xml_served():
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "application/xml" in r.headers["content-type"]
    assert "<urlset" in r.text
    # Landing URL listed with hreflang alternates
    assert "<loc>https://aib.zapleo.com/</loc>" in r.text
    assert 'hreflang="en"' in r.text and 'hreflang="ru"' in r.text
    # /docs surfaced
    assert "<loc>https://aib.zapleo.com/docs</loc>" in r.text


def test_llms_txt_served():
    """Jeremy Howard's proposed /llms.txt — LLM-friendly site descriptor."""
    r = client.get("/llms.txt")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    # Standard markdown structure: # title + > summary + sections
    assert r.text.startswith("# AIbroker")
    assert "> Open-source" in r.text
    # Key concepts documented
    for kw in ("Two operating modes", "Capabilities", "Scopes",
               "Adaptive cooldown", "Reserved lane"):
        assert kw in r.text
    # GitHub repo + license
    assert "github.com/zapleoceo/AIbroker" in r.text
    assert "License: MIT" in r.text


def test_landing_shows_version():
    """{version} placeholder is interpolated."""
    from aibroker import __version__
    r = client.get("/")
    assert f"v{__version__}" in r.text
