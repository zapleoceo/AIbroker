"""Public health endpoints — no auth. Landing page lives in routes/landing.py."""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape as esc
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from aibroker import __version__
from aibroker.auth_session import require_owner_session
from aibroker.db import get_session
from aibroker.routes.landing import CSS_LINKS, FAVICON_LINKS

router = APIRouter(tags=["health"])

# /v1/health reflects live key state (monitor ticks every 10min, cooldowns
# resolve continuously) — never let a browser/CDN serve a stale snapshot
# (regression class: dashboard once showed 77 keys after DB had 51, root
# cause was exactly this kind of missing no-store).
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


@router.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "service": "aibroker", "ts": datetime.now(UTC).isoformat()}


async def _fetch_provider_health() -> list[dict[str, Any]]:  # pragma: no cover
    """Per-provider alive/cooldown/dead/total counts — single source of truth
    for both the JSON and the browser-rendered view of /v1/health.

    Postgres-only (now()/FILTER) — exercised by the Postgres-only
    test_v1_health_* tests, not the SQLite diff-cover run, hence the pragma."""
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT provider, "
                    "       COUNT(*) FILTER (WHERE is_active AND is_alive "
                    "                         AND (cooldown_until IS NULL OR cooldown_until < now())) AS alive, "
                    "       COUNT(*) FILTER (WHERE is_active AND is_alive AND cooldown_until > now()) AS cooldown, "
                    "       COUNT(*) FILTER (WHERE NOT is_alive OR NOT is_active) AS dead, "
                    "       COUNT(*) AS total "
                    "FROM api_keys GROUP BY provider ORDER BY provider"
                )
            )
        ).all()
    return [
        {"provider": r[0], "alive": r[1], "cooldown": r[2], "dead": r[3], "total": r[4]}
        for r in rows
    ]


def _is_privileged(request: Request) -> bool:
    """True for a valid X-Admin-Key or owner session cookie (same check as the
    dashboard). RuntimeError = SESSION_SECRET unset with a cookie present."""
    try:
        require_owner_session(request)
    except (HTTPException, RuntimeError):
        return False
    return True


def _aggregate_health(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse per-provider rows into ONE row — what an anonymous caller gets.

    2026-10-02 review: the public endpoint listed which providers the broker
    holds keys for and how many are dead/cooling, i.e. a map of where to push
    to exhaust the free pool. Uptime checks only need "is anything alive", so
    anonymous callers get totals; the per-provider rows need admin/owner auth.
    The top-level `providers` key is kept (one row, provider "all") so existing
    consumers keep parsing."""
    return [{
        "provider": "all",
        "alive": sum(p["alive"] for p in providers),
        "cooldown": sum(p["cooldown"] for p in providers),
        "dead": sum(p["dead"] for p in providers),
        "total": sum(p["total"] for p in providers),
    }]


# Reused verbatim from landing.py's lang-toggle (no {}-interpolation needed on
# this block, so it's a plain string — no .format()/f-string brace escaping).
_LANG_TOGGLE_JS = """
<script>
(function() {
  const KEY = "aib_lang";
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("lang");
  let fromStore = null;
  try { fromStore = localStorage.getItem(KEY); } catch (e) {}
  let lang = (fromQuery === "ru" || fromQuery === "en") ? fromQuery
            : (fromStore === "ru" || fromStore === "en") ? fromStore
            : "en";
  function apply(l) {
    document.documentElement.lang = l;
    document.querySelectorAll("[data-i18n]").forEach(el => {
      const txt = el.getAttribute("data-" + l);
      if (txt !== null) el.textContent = txt;
    });
    document.querySelectorAll(".lang-toggle button").forEach(b => {
      b.classList.toggle("active", b.dataset.lang === l);
    });
    try { localStorage.setItem(KEY, l); } catch (e) {}
  }
  document.querySelectorAll(".lang-toggle button").forEach(b => {
    b.addEventListener("click", () => apply(b.dataset.lang));
  });
  apply(lang);
})();
</script>
"""

def _health_provider_card(p: dict[str, Any]) -> str:
    total = p["total"] or 1  # guard div-by-zero; total is always >=1 per row's own GROUP BY
    def pct(n: int) -> float:
        return round(n / total * 100, 2)
    bar = "".join(
        f'<span class="seg-{cls}" style="width:{pct(n)}%"></span>'
        for cls, n in (("good", p["alive"]), ("warn", p["cooldown"]), ("bad", p["dead"]))
        if n
    ) or '<span class="seg-empty" style="width:100%"></span>'
    return f"""
    <div class="pcard">
      <div class="name">{esc(p["provider"])}</div>
      <div class="bar">{bar}</div>
      <div class="stats">
        <span class="good">{p["alive"]} <b data-i18n data-en="alive" data-ru="живы">alive</b></span>
        <span class="warn">{p["cooldown"]} <b data-i18n data-en="cooldown" data-ru="пауза">cooldown</b></span>
        <span class="bad">{p["dead"]} <b data-i18n data-en="dead" data-ru="мертвы">dead</b></span>
        <span>{p["total"]} <b data-i18n data-en="total" data-ru="всего">total</b></span>
      </div>
    </div>"""


def _render_health_html(providers: list[dict[str, Any]]) -> HTMLResponse:
    alive = sum(p["alive"] for p in providers)
    cooldown = sum(p["cooldown"] for p in providers)
    dead = sum(p["dead"] for p in providers)
    total = sum(p["total"] for p in providers)
    cards = "".join(_health_provider_card(p) for p in providers) or (
        '<div class="empty" data-i18n data-en="No keys configured yet." '
        'data-ru="Ключи ещё не настроены.">No keys configured yet.</div>'
    )
    body = f"""<!doctype html><html><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIbroker — provider health</title>
{FAVICON_LINKS}
{CSS_LINKS}
</head><body class="health">
<header><div class="nav">
  <a href="/" class="brand"><span class="dot"></span> AIbroker</a>
  <div class="nav-right">
    <div class="lang-toggle">
      <button data-lang="en" class="active">EN</button>
      <button data-lang="ru">RU</button>
    </div>
    <a href="/dashboard" data-i18n data-en="Dashboard →" data-ru="Панель →">Dashboard →</a>
  </div>
</div></header>
<div class="container">
  <h1 data-i18n data-en="Provider health" data-ru="Статус провайдеров">Provider health</h1>
  <p class="sub">
    <span data-i18n
          data-en="Live count of api_keys by state, per provider. Machine-readable form:"
          data-ru="Живой подсчёт api_keys по состоянию, по провайдерам. Машиночитаемая форма:">
      Live count of api_keys by state, per provider. Machine-readable form:
    </span>
    <code>curl -H "Accept: application/json" /v1/health</code>
  </p>
  <div class="totals">
    <div class="tstat good"><div class="n">{alive}</div>
      <div class="l" data-i18n data-en="alive" data-ru="живы">alive</div></div>
    <div class="tstat warn"><div class="n">{cooldown}</div>
      <div class="l" data-i18n data-en="cooldown" data-ru="пауза">cooldown</div></div>
    <div class="tstat bad"><div class="n">{dead}</div>
      <div class="l" data-i18n data-en="dead" data-ru="мертвы">dead</div></div>
    <div class="tstat"><div class="n">{total}</div>
      <div class="l" data-i18n data-en="total keys" data-ru="всего ключей">total keys</div></div>
  </div>
  <div class="grid">{cards}</div>
</div>
<footer>AIbroker v{esc(__version__)} · <a href="/">aib.zapleo.com</a></footer>
{_LANG_TOGGLE_JS}
</body></html>"""
    return HTMLResponse(body, headers=_NO_STORE)


@router.get("/v1/health")
async def health_summary(request: Request) -> Response:  # pragma: no cover
    """Alive/dead/cooldown counts — per provider for an admin key / owner
    session, a single aggregate row (provider "all") for anonymous callers.

    Content-negotiated: a browser (Accept: text/html, e.g. clicking the
    dashboard nav link) gets a colored status page; anything else (curl,
    scripts, uptime monitors, no Accept header) gets the plain JSON contract
    this endpoint has always returned — unchanged, so existing programmatic
    consumers (documented publicly on the landing page and in docs/api.md)
    never see a shape change.

    Postgres-only (via _fetch_provider_health) — exercised by the Postgres-
    only test_v1_health_returns_providers_array / test_v1_health_html_for_
    browser_accept, not the SQLite diff-cover run, hence the pragma. The
    content-negotiation branch and both render paths are separately unit-
    tested SQLite-safe via _render_health_html/_health_provider_card."""
    providers = await _fetch_provider_health()
    detail = _is_privileged(request)
    if not detail:
        providers = _aggregate_health(providers)
    if "text/html" in request.headers.get("accept", ""):
        return _render_health_html(providers)
    return JSONResponse({"providers": providers, "detail": detail}, headers=_NO_STORE)
