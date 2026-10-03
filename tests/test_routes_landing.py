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
    for p in ["cerebras", "groq", "gemini", "cohere",
               "openrouter", "voyage", "deepseek", "anthropic", "openai", "local"]:
        assert p in r.text


def test_favicon_svg_served():
    r = client.get("/favicon.svg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.text.startswith("<svg ")
    # Brand colours present
    assert "#4dabf7" in r.text
    assert "#0b0d11" in r.text


def test_favicon_ico_served_as_svg_fallback():
    """Browsers requesting /favicon.ico by default — same content, modern
    browsers accept image/svg+xml at any path."""
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/svg+xml"
    assert r.text.startswith("<svg ")


def test_favicon_cached_24h():
    """Cache-Control header set so browsers don't re-fetch on every page."""
    r = client.get("/favicon.svg")
    assert "max-age=86400" in r.headers.get("cache-control", "")


def test_landing_links_to_favicon():
    """Landing <head> includes the favicon refs (modern + fallback + apple)."""
    r = client.get("/")
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in r.text
    assert '<link rel="alternate icon" href="/favicon.ico">' in r.text
    assert '<link rel="apple-touch-icon" href="/favicon.svg">' in r.text


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
    assert "> Self-hosted" in r.text
    # Key concepts documented
    for kw in ("Proxy mode", "Capabilities", "Scopes",
               "Adaptive cooldown", "Reserved lane"):
        assert kw in r.text
    # GitHub repo + license — must match README.md's own "Proprietary,
    # owner: zapleoceo" (2026-07-03: the site used to claim MIT-style in
    # three places, directly contradicting README; there's no LICENSE file).
    assert "github.com/zapleoceo/AIbroker" in r.text
    assert "License: Proprietary" in r.text
    assert "MIT" not in r.text


def test_landing_shows_version():
    """{version} placeholder is interpolated."""
    from aibroker import __version__
    r = client.get("/")
    assert f"v{__version__}" in r.text


# ─── 2026-10-02 site review: landing copy matches the code ──────────────────


def _page_and_llms() -> tuple[str, str]:
    return client.get("/").text, client.get("/llms.txt").text


def test_landing_license_stat_is_source_available_not_open_source():
    """L1: the hero said '100% Open source' while JSON-LD/llms.txt say proprietary."""
    page, _ = _page_and_llms()
    assert 'data-en="Source-available"' in page
    assert "Open source" not in page and "Открытый код" not in page


def test_landing_privacy_answer_admits_async_job_payloads_are_kept():
    """L2: bodies of async jobs sit in deep_jobs until the retention purge."""
    page, llms = _page_and_llms()
    assert "forgotten" not in page and "забываются" not in page
    assert "purged after 7 days" in page and "purged after 7 days" in llms
    assert "через 7 дней" in page


def test_landing_has_no_vending_mode():
    """L3: vending/leases were removed 2026-07-12."""
    page, llms = _page_and_llms()
    assert "vending" not in page.lower() and "vending" not in llms.lower()
    assert "Two modes" not in page
    assert "Active leases" not in page
    assert "Async jobs and sync endpoints" in page


def test_landing_provider_count_is_consistent_with_tiles():
    """L4/L5: one count everywhere; no GitHub Models; no Mistral; `local` shown."""
    import re
    page, llms = _page_and_llms()
    tiles = re.findall(r'<div class="prov">', page)
    from aibroker.routes.landing import wired_providers
    n = len(wired_providers())
    assert len(tiles) == n == 14
    assert f"{n} providers" in page
    assert "Fifteen" not in page and "15 providers" not in page
    assert "15 LLM providers" not in page and "15 LLM providers" not in llms
    for text in (page, llms):
        assert "GitHub Models" not in text
        assert "mistral" not in text.lower()
    assert '<div class="prov">local ' in page


def test_landing_chat_smart_is_free_gemini_first_with_deepseek_fallback():
    """L6: chat:smart = gemini -> deepseek -> sambanova -> anthropic."""
    page, llms = _page_and_llms()
    assert "leads with DeepSeek" not in page and "DeepSeek-led" not in llms
    assert "free Gemini first" in page and "free Gemini first" in llms
    from aibroker.routing.chains import CAPABILITY_CHAINS
    assert CAPABILITY_CHAINS["chat:smart"][:2] == ["gemini", "deepseek"]


def test_landing_transcription_is_not_promised_instant():
    """L7: the self-hosted whisper fallback takes minutes (131-168s observed)."""
    page, _ = _page_and_llms()
    assert "never hit proxy timeouts" not in page
    assert "/v1/transcribe/jobs" in page


def test_landing_api_section_lists_every_client_endpoint_and_scope():
    """L8: heading said three groups but showed four; several endpoints/scopes missing."""
    page, llms = _page_and_llms()
    assert "Four endpoint groups" in page and "Three endpoint groups" not in page
    for path in ("/v1/decisions", "/v1/deep", "/v1/deep/{job_id}", "/v1/transcribe/jobs",
                 "/admin/keys/{id}"):
        assert path in page, path
    assert "410 Gone" in page
    for scope in ("llm:edit", "llm:deep", "llm:audio", "llm:decision"):
        assert scope in page and scope in llms, scope
    assert "/v1/decisions" in llms and "410 Gone" in llms


def test_landing_curl_examples_send_a_json_content_type():
    """L9: curl -d defaults to form-encoding; FastAPI answers 422 without the header."""
    page, _ = _page_and_llms()
    assert page.count('-H "Content-Type: application/json"') >= 2


def test_landing_cache_discount_claims_are_per_provider_and_no_rotatable_secret():
    """L10: DeepSeek cache reads are ~0.02x ($0.003 vs $0.15 per M), not ~0.1x."""
    page, _ = _page_and_llms()
    assert "~0.02x on DeepSeek" in page and "~0.1x on Anthropic" in page
    assert "rotatable" not in page and "ротируемым" not in page


def test_landing_does_not_leak_internal_env_var_names():
    """L11: OWNER_TELEGRAM_ID / TOKEN_SECRET are deployment internals."""
    page, llms = _page_and_llms()
    for text in (page, llms):
        assert "OWNER_TELEGRAM_ID" not in text
        assert "TOKEN_SECRET" not in text
        assert "JOB_RETENTION_DAYS" not in text
    assert "X-Admin-Key" in page


def test_landing_survives_blocked_storage_and_has_noscript_summary():
    """L12: an uncaught localStorage throw aborted the script -> blank page."""
    page = client.get("/").text
    assert "<noscript>" in page
    assert "/docs" in page.split("<noscript>")[1].split("</noscript>")[0]
    assert "try { fromStore = localStorage.getItem(KEY); } catch (e) {}" in page
    assert "try { localStorage.setItem(KEY, l); } catch (e) {}" in page
    # no bare storage call is left outside a try
    assert page.count("localStorage.") == 2


def test_landing_accepts_head():
    """L13: HEAD / was 405 (uptime checkers, link unfurlers)."""
    r = client.head("/")
    assert r.status_code == 200


def _contrast(fg: str, bg: str) -> float:
    def lum(h: str) -> float:
        h = h.lstrip("#")
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    hi, lo = sorted((lum(fg), lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_landing_dim_text_meets_wcag_aa_on_the_page_background():
    """L13 (tokens era): --dim must stay >= 4.5:1 on its surface in light AND dark."""
    import re
    tokens = client.get("/dashboard/static/css/tokens.css").text
    light, dark = tokens.split("@media (prefers-color-scheme: dark)")
    for block in (light, dark):
        dim = re.search(r"--dim:\s*(#[0-9a-f]{6});", block).group(1)
        bg = re.search(r"--bg:\s*(#[0-9a-f]{6});", block).group(1)
        assert _contrast(dim, bg) >= 4.5, (dim, bg)
    assert _contrast("#5a6171", "#0b0d11") < 4.5   # the helper discriminates


# ─── generated from the routing tables (no copy drift) ──────────────────────


def test_landing_provider_list_is_the_wired_set_and_excludes_mistral():
    from aibroker.routes.landing import wired_providers
    wired = wired_providers()
    assert "mistral" not in wired and "deepseek" in wired and "local" in wired
    body = client.get("/").text
    assert "mistral" not in body.lower().split('id="providers"')[1].split("</section>")[0]
    assert f'<div class="num">{len(wired)}</div>' in body
    assert "{n_providers}" not in body


def test_landing_lists_every_client_route_that_is_actually_routed():
    from aibroker.routes import proxy
    body = client.get("/").text
    for r in proxy.router.routes:
        assert "/v1" + r.path in body, r.path
    for path in ("/v1/decisions", "/v1/transcribe", "/v1/transcribe/jobs", "/v1/deep"):
        assert path in body


def test_llms_txt_is_generated_and_names_every_capability_and_scope():
    from aibroker.routing.chains import CAPABILITY_CHAINS, CAPABILITY_SCOPE
    txt = client.get("/llms.txt").text
    assert "@@" not in txt
    for cap in CAPABILITY_CHAINS:
        assert f"`{cap}`" in txt
    for scope in set(CAPABILITY_SCOPE.values()):
        assert scope in txt


def test_public_pages_share_the_design_tokens_stylesheet():
    body = client.get("/").text
    assert "/dashboard/static/css/tokens.css" in body and "<style>" not in body
