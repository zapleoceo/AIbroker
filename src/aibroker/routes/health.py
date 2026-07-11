"""Public health endpoints — no auth. Landing page lives in routes/landing.py."""
from __future__ import annotations

from datetime import UTC, datetime
from html import escape as esc
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text

from aibroker import __version__
from aibroker.db import get_session
from aibroker.routes.landing import FAVICON_LINKS

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


# Reused verbatim from landing.py's lang-toggle (no {}-interpolation needed on
# this block, so it's a plain string — no .format()/f-string brace escaping).
_LANG_TOGGLE_JS = """
<script>
(function() {
  const KEY = "aib_lang";
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("lang");
  const fromStore = localStorage.getItem(KEY);
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
    localStorage.setItem(KEY, l);
  }
  document.querySelectorAll(".lang-toggle button").forEach(b => {
    b.addEventListener("click", () => apply(b.dataset.lang));
  });
  apply(lang);
})();
</script>
"""

_HEALTH_CSS = """
:root {
  --bg:#0b0d11; --panel:#13161c; --panel2:#191d25; --line:#262a33;
  --text:#e6e8ec; --muted:#8b929f; --dim:#5a6171;
  --accent:#4dabf7; --accent-soft:rgba(77,171,247,.12);
  --good:#51cf66; --warn:#ffd43b; --bad:#ff6b6b;
  --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0;background:var(--bg);color:var(--text);
  font-family:var(--sans);line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.container{max-width:900px;margin:0 auto;padding:0 24px 64px}
header{padding:20px 0;border-bottom:1px solid var(--line);margin-bottom:32px}
.nav{display:flex;align-items:center;justify-content:space-between;
  max-width:900px;margin:0 auto;padding:0 24px}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:17px}
.brand .dot{width:9px;height:9px;background:var(--accent);
  border-radius:50%;box-shadow:0 0 12px var(--accent)}
.nav-right{display:flex;align-items:center;gap:14px}
.lang-toggle{display:flex;background:var(--panel);border:1px solid var(--line);
  border-radius:6px;overflow:hidden;font-family:var(--mono);font-size:12px}
.lang-toggle button{background:none;border:none;color:var(--muted);
  padding:6px 12px;cursor:pointer;font-family:var(--mono);font-size:12px}
.lang-toggle button.active{background:var(--accent-soft);color:var(--accent)}
h1{font-weight:600;font-size:28px;margin:0 0 6px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px;margin:0 0 32px}
.sub code{font-family:var(--mono);background:var(--panel);padding:2px 6px;
  border-radius:4px;color:var(--accent)}
.totals{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
  gap:12px;margin-bottom:32px}
.tstat{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px}
.tstat .n{font-size:24px;font-weight:600;font-family:var(--mono)}
.tstat .l{font-size:11px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.05em;margin-top:2px}
.tstat.good .n{color:var(--good)} .tstat.warn .n{color:var(--warn)}
.tstat.bad .n{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  gap:12px}
.pcard{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:14px 16px}
.pcard .name{font-family:var(--mono);font-size:14px;font-weight:600;
  margin-bottom:10px}
.pcard .bar{display:flex;height:8px;border-radius:4px;overflow:hidden;
  background:#0000;margin-bottom:10px}
.pcard .bar span{display:block;height:100%}
.pcard .seg-good{background:var(--good)} .pcard .seg-warn{background:var(--warn)}
.pcard .seg-bad{background:var(--bad)} .pcard .seg-empty{background:var(--line)}
.pcard .stats{display:flex;gap:12px;font-size:12px;color:var(--muted);
  flex-wrap:wrap}
.pcard .stats b{font-family:var(--mono)}
.pcard .stats .good b{color:var(--good)} .pcard .stats .warn b{color:var(--warn)}
.pcard .stats .bad b{color:var(--bad)}
.empty{color:var(--dim);padding:32px 0;text-align:center}
footer{max-width:900px;margin:32px auto 0;padding:0 24px;color:var(--dim);
  font-size:12px}
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
<style>{_HEALTH_CSS}</style>
</head><body>
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
    """Per-provider alive/dead/cooldown counts.

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
    if "text/html" in request.headers.get("accept", ""):
        return _render_health_html(providers)
    return JSONResponse({"providers": providers}, headers=_NO_STORE)
