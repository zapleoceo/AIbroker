"""Browser admin UI — Telegram login, dashboard, inline forms for CRUD."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, date, datetime, timedelta
from html import escape as esc
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, text

from aibroker.auth_session import (
    COOKIE_NAME,
    OwnerSession,
    issue_session_cookie,
    require_owner_session,
    verify_telegram_widget,
)
from aibroker.config import get_settings
from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.providers.auto_discover import discover_and_store
from aibroker.providers.litellm_adapter import DEFAULT_MODEL
from aibroker.providers.quotas import axes_for_key, severity_class
from aibroker.telemetry import audit

# ─── Provider catalogue (drives add-key form dropdown) ──────────────────────


def _provider_catalogue() -> list[dict[str, Any]]:
    """One entry per known provider: name, capabilities, default scope.

    Sorted by free-first usefulness: cerebras/groq/gemini/openrouter/deepseek
    first (free or cheap), then paid (openai/anthropic), then voyage (embed-only).
    """
    # capability → scope mapping; voyage embeddings → llm:embed, everything else → llm:chat
    def scope_for(caps: list[str]) -> str:
        if "embedding" in caps:
            return "llm:embed"
        if "vision" in caps and len(caps) == 1:
            return "llm:vision"
        if "chat:deep" in caps and len(caps) == 1:
            return "llm:deep"
        return "llm:chat"

    order = ["cerebras", "groq", "gemini", "mistral", "cohere",
             "openrouter", "deepseek",
             "openai", "anthropic", "voyage",
             "sambanova", "github", "nvidia", "cloudflare"]
    out = []
    for p in order:
        caps = list(DEFAULT_MODEL.get(p, {}).keys())
        if not caps:
            continue
        out.append({
            "provider": p,
            "capabilities": caps,
            "default_scope": scope_for(caps),
            "models": DEFAULT_MODEL[p],
        })
    return out


def _provider_meta_json() -> str:
    """Compact JSON for the in-page <script> — what JS reads on provider change."""
    import json
    return json.dumps({p["provider"]: p for p in _provider_catalogue()},
                       separators=(",", ":"))

router = APIRouter(tags=["dashboard"])

# Authenticated, always-fresh admin pages must never be cached — without this
# Chrome heuristic-caches the HTML (CF already serves it DYNAMIC) and shows a
# stale key list. Applied to every dashboard/login HTMLResponse.
_NO_STORE = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}


# ─── Login ──────────────────────────────────────────────────────────────────


_LOGIN_HTML = """<!doctype html><html><head>
<meta charset="utf-8"><title>AIbroker — login</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="alternate icon" href="/favicon.ico">
<style>
  body { font-family:-apple-system, sans-serif; background:#0f1115; color:#e4e6eb;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .box { background:#1a1d24; padding:48px 40px; border-radius:14px; max-width:420px;
          text-align:center; border:1px solid #2a2d34; position:relative; }
  h1 { font-weight:500; font-size:28px; margin:0 0 8px; }
  p { color:#888; margin:6px 0 24px; font-size:14px; }
  .err { color:#f44336; margin-top:18px; font-size:13px; }
  .lang-toggle { position:absolute; top:14px; right:14px;
                display:inline-flex; background:#0f1115; border:1px solid #2a2d34;
                border-radius:6px; overflow:hidden;
                font-family:ui-monospace,monospace; font-size:11px; }
  .lang-toggle button { background:none; border:none; color:#888;
                       padding:5px 10px; cursor:pointer;
                       font-family:ui-monospace,monospace; font-size:11px; }
  .lang-toggle button.active { background:rgba(77,171,247,.12); color:#4dabf7; }
</style></head><body>
<div class="box">
  <span class="lang-toggle">
    <button type="button" data-lang="en" class="active">EN</button>
    <button type="button" data-lang="ru">RU</button>
  </span>
  <h1>AIbroker</h1>
  <p data-i18n
     data-en="Sign in with Telegram to administer"
     data-ru="Войти через Telegram, чтобы администрировать">Sign in with Telegram to administer</p>
  <script async src="https://telegram.org/js/telegram-widget.js?22"
          data-telegram-login="__BOT__"
          data-size="large"
          data-radius="8"
          data-auth-url="https://__HOST__/api/tg_login"
          data-request-access="write"></script>
  __ERR__
</div>
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
</body></html>"""


@router.get("/login", response_class=HTMLResponse)
async def login_page(error: str | None = None) -> HTMLResponse:
    s = get_settings()
    bot = s.TELEGRAM_BOT_USERNAME or "telegram"
    err_html = f'<div class="err">{esc(error)}</div>' if error else ""
    return HTMLResponse(
        _LOGIN_HTML.replace("__BOT__", bot)
                   .replace("__HOST__", s.PUBLIC_HOST)
                   .replace("__ERR__", err_html),
        headers=_NO_STORE,
    )


@router.get("/api/tg_login")
async def tg_login_callback(request: Request) -> RedirectResponse:
    s = get_settings()
    qp = dict(request.query_params)
    user_id = verify_telegram_widget(qp)
    if user_id is None:
        return RedirectResponse("/login?error=Invalid+Telegram+signature", status_code=303)
    if user_id != s.OWNER_TELEGRAM_ID:
        return RedirectResponse(
            f"/login?error=Access+denied+for+user+{user_id}", status_code=303
        )
    cookie, ttl = issue_session_cookie(user_id)
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(
        COOKIE_NAME, cookie,
        max_age=ttl, httponly=True, secure=True, samesite="lax", path="/",
    )
    await audit(actor=f"tg:{user_id}", action="login.success", ip=_ip(request))
    return resp


@router.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ─── Dashboard render ───────────────────────────────────────────────────────


def _dash_html(*, body: str, flash: str = "") -> str:
    return f"""<!doctype html><html><head>
<meta charset="utf-8"><title>AIbroker</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="alternate icon" href="/favicon.ico">
<style>
body {{ font-family:-apple-system, sans-serif; background:#0f1115; color:#e4e6eb;
       margin:0; padding:24px; max-width:1280px; margin-inline:auto; }}
nav {{ display:flex; gap:24px; align-items:center; margin-bottom:24px; }}
nav h1 {{ margin:0; font-weight:500; font-size:22px; }}
nav .right {{ margin-left:auto; }}
nav a {{ color:#4dabf7; text-decoration:none; font-size:13px; margin-left:14px; }}
h2 {{ font-weight:500; font-size:18px; margin:32px 0 12px; color:#aaa; }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
         gap:12px; margin:14px 0; }}
.card {{ background:#1a1d24; padding:16px; border-radius:10px; border:1px solid #2a2d34; }}
.card-label {{ font-size:10px; color:#888; text-transform:uppercase; letter-spacing:.05em; }}
.card-value {{ font-size:26px; font-weight:600; margin-top:4px; }}
.card-sub {{ font-size:12px; color:#888; margin-top:2px; }}
table {{ width:100%; border-collapse:collapse; background:#1a1d24; border-radius:10px;
         overflow:hidden; margin:12px 0; }}
th, td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #2a2d34; font-size:13px; }}
th {{ background:#0f1115; color:#888; text-transform:uppercase; font-size:11px; font-weight:500; }}
td.mono, code {{ font-family:ui-monospace, monospace; color:#4dabf7; font-size:12px; }}
.ok {{ color:#4caf50; }} .bad {{ color:#f44336; }} .warn {{ color:#ffd84a; }}
.pill {{ display:inline-block; padding:2px 8px; border-radius:8px; font-size:11px;
         background:#0f1115; border:1px solid #2a2d34; }}
form.inline {{ display:inline; }}
button, input, select {{ font:inherit; }}
button {{ background:#1a1d24; color:#e4e6eb; border:1px solid #2a2d34; border-radius:6px;
          padding:5px 10px; font-size:12px; cursor:pointer; }}
button:hover {{ background:#2a2d34; }}
button.danger {{ color:#f44336; }}
input, select {{ background:#0f1115; color:#e4e6eb; border:1px solid #2a2d34;
                 border-radius:6px; padding:6px 10px; font-size:13px; }}
.flash {{ background:#1a3d1a; border:1px solid #2a5d2a; color:#4caf50; padding:10px 14px;
          border-radius:8px; margin-bottom:18px; font-size:13px; }}
.flash.err {{ background:#3d1a1a; border-color:#5d2a2a; color:#f44336; }}
fieldset {{ background:#1a1d24; border:1px solid #2a2d34; border-radius:10px;
            padding:14px 18px; margin:12px 0; }}
legend {{ color:#aaa; padding:0 8px; font-size:12px; }}
.row-form {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:6px; }}
.row-form input, .row-form select {{ min-width:120px; }}
.provider {{ display:inline-block; margin:4px 6px 4px 0; padding:5px 10px;
             background:#1a1d24; border:1px solid #2a2d34; border-radius:6px; font-size:12px; }}
/* Sortable headers */
th.sortable {{ cursor:pointer; user-select:none; position:relative; padding-right:18px; }}
th.sortable:hover {{ color:#e4e6eb; background:#13161c; }}
th.sortable::after {{ content:"↕"; position:absolute; right:6px; opacity:.3; font-size:10px; }}
th.sortable.asc::after  {{ content:"↑"; opacity:1; color:#4dabf7; }}
th.sortable.desc::after {{ content:"↓"; opacity:1; color:#4dabf7; }}
/* Inline edit row */
tr.editing td {{ background:#13161c; }}
tr.edit-row {{ display:none; }}
tr.edit-row.active {{ display:table-row; background:#13161c; }}
tr.edit-row td {{ padding:14px 12px; }}
tr.edit-row input, tr.edit-row select {{ min-width:90px; }}
/* Scope checkboxes */
.scope-group {{ display:inline-flex; gap:8px; flex-wrap:wrap;
              padding:4px 8px; border:1px solid #2a2d34; border-radius:6px;
              background:#0f1115; }}
.scope-group .scope-cb {{ display:inline-flex; align-items:center; gap:4px;
                         font-family:ui-monospace,monospace; font-size:11px;
                         color:#888; cursor:pointer; }}
.scope-group .scope-cb input {{ margin:0; min-width:0; padding:0; }}
.scope-group .scope-cb input:checked + * {{ color:#4dabf7; }}
/* Manual quota override cluster (4 narrow number inputs) */
.quota-override {{ display:inline-flex; gap:4px; align-items:center;
                 padding:3px 6px; border:1px dashed #2a2d34; border-radius:6px; }}
.quota-override input {{ width:78px; min-width:0; font-size:11px;
                       font-family:ui-monospace,monospace; padding:4px 6px; }}
/* Cap bar */
.cap-bar {{ display:inline-block; width:80px; height:6px; background:#0f1115;
            border-radius:3px; vertical-align:middle; margin-left:6px; overflow:hidden; }}
.cap-bar .fill {{ display:block; height:100%; background:#4dabf7; }}
.cap-bar .fill.warn {{ background:#ffd84a; }}
.cap-bar .fill.bad  {{ background:#f44336; }}
/* Project drill-down */
.proj-link {{ color:#e4e6eb; text-decoration:none; border-bottom:1px dotted #4dabf7; }}
.proj-link:hover {{ color:#4dabf7; }}
.breadcrumb {{ font-size:12px; color:#888; margin:6px 0 18px; }}
.breadcrumb a {{ color:#4dabf7; }}
.breakdown {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
            gap:12px; margin:8px 0 20px; }}
.brk-card {{ background:#1a1d24; border:1px solid #2a2d34; border-radius:10px;
           padding:14px 16px; }}
.brk-card h3 {{ margin:0 0 10px; font-size:11px; color:#888;
              text-transform:uppercase; letter-spacing:.05em; font-weight:500; }}
.brk-card table {{ margin:0; background:none; border-radius:0; }}
.brk-card td {{ padding:4px 8px 4px 0; border:none; font-size:12px; color:#aaa; }}
.brk-card td.num {{ color:#e4e6eb; text-align:right; font-family:ui-monospace,monospace; }}
.brk-card td.k {{ color:#4dabf7; font-family:ui-monospace,monospace; }}
.brk-card .total-row {{ border-top:1px solid #2a2d34; }}
.brk-card .total-row td {{ padding-top:6px; color:#e4e6eb; font-weight:600; }}
.range-pills {{ display:inline-flex; gap:6px; margin-left:10px; vertical-align:middle; }}
.range-pills a {{ font-size:11px; padding:3px 9px; border-radius:4px;
               background:#1a1d24; border:1px solid #2a2d34;
               color:#888; text-decoration:none; font-family:ui-monospace,monospace; }}
.range-pills a.active {{ background:rgba(77,171,247,.12); color:#4dabf7;
                       border-color:#4dabf7; }}
.recent-table td.status-ok {{ color:#4caf50; }}
.recent-table td.status-error {{ color:#f44336; }}
.recent-table td.status-rate_limit {{ color:#ffd84a; }}
.recent-table td.status-auth_fail {{ color:#ff8a00; }}
/* Date-range picker on main dashboard */
.range-form {{ display:flex; align-items:center; gap:8px; margin:0 0 14px;
              flex-wrap:wrap; font-size:13px; color:#888; }}
.range-form label {{ font-family:ui-monospace,monospace; font-size:11px;
                    text-transform:uppercase; letter-spacing:.05em; color:#5a6171; }}
.range-form input[type="date"] {{ font-family:ui-monospace,monospace;
                                 font-size:12px; padding:5px 8px; min-width:130px; }}
.range-form .range-reset {{ color:#888; font-size:12px; text-decoration:none;
                           font-family:ui-monospace,monospace; padding:5px 10px;
                           border:1px solid #2a2d34; border-radius:6px; }}
.range-form .range-quick {{ margin-left:10px; display:inline-flex; gap:6px; }}
.range-form .range-quick a {{ font-size:11px; padding:3px 9px; border-radius:4px;
                             background:#1a1d24; border:1px solid #2a2d34;
                             color:#888; text-decoration:none;
                             font-family:ui-monospace,monospace; }}
.range-form .range-quick a:hover {{ color:#4dabf7; border-color:#4dabf7; }}
/* Selected-range indicator: 'all time' and the quick today/7d/30d links share
   the same active look (regression: 'all time' used to render permanently
   blue regardless of state, and today/7d/30d never got any indicator). */
.range-form .range-reset.active,
.range-form .range-quick a.active {{ background:rgba(77,171,247,.12);
                                    color:#4dabf7; border-color:#4dabf7; }}
/* Totals row */
tfoot td {{ background:#0f1115; font-weight:600; color:#e4e6eb;
           border-top:2px solid #2a2d34; padding:10px 12px; font-size:12px; }}
tfoot td.k {{ color:#888; text-transform:uppercase; letter-spacing:.05em;
             font-size:11px; }}
tfoot td.num {{ font-family:ui-monospace,monospace; color:#4dabf7; }}
/* Row number — CSS counter so it stays correct after client-side re-sort.
   id is the DB identifier (has gaps from deletions); # is the visible count. */
tbody {{ counter-reset: rownum; }}
tr.data-row {{ counter-increment: rownum; }}
td.rownum {{ width:30px; text-align:right; color:#5a6171;
           font-family:ui-monospace,monospace; font-size:11px; }}
td.rownum::before {{ content: counter(rownum); }}
/* Provider hint under add-key form */
.provider-hint {{ margin-top:10px; padding:10px 12px; border-radius:6px;
                background:#0f1115; border:1px dashed #2a2d34;
                color:#888; font-size:12px; line-height:1.55; }}
.provider-hint b {{ color:#4dabf7; font-family:ui-monospace,monospace; font-weight:500; }}
.provider-hint .cap-tag {{ display:inline-block; margin:2px 4px 2px 0; padding:1px 7px;
                         font-family:ui-monospace,monospace; font-size:11px;
                         background:#1a1d24; color:#4dabf7; border-radius:3px; }}
.provider-hint table {{ width:auto; background:none; margin:6px 0 0; border-radius:0; overflow:visible; }}
.provider-hint td {{ padding:2px 12px 2px 0; border:none; font-size:12px; color:#aaa; }}
.provider-hint td.mono {{ font-family:ui-monospace,monospace; color:#4dabf7; }}
/* Lang toggle */
.lang-toggle {{ display:inline-flex; background:#1a1d24; border:1px solid #2a2d34;
              border-radius:6px; overflow:hidden; font-family:ui-monospace,monospace;
              font-size:11px; margin-right:10px; vertical-align:middle; }}
.lang-toggle button {{ background:none; border:none; color:#888;
              padding:5px 10px; cursor:pointer; font-family:ui-monospace,monospace;
              font-size:11px; }}
.lang-toggle button.active {{ background:rgba(77,171,247,.12); color:#4dabf7; }}
[data-i18n].lang-hidden {{ display:none !important; }}
</style></head><body>

<nav>
  <h1>AIbroker</h1>
  <span class="pill">v0.1.0</span>
  <span class="right">
    <span class="lang-toggle">
      <button type="button" data-lang="en" class="active">EN</button>
      <button type="button" data-lang="ru">RU</button>
    </span>
    <a href="/v1/health">/v1/health</a>
    <a href="/docs">/docs</a>
    <a href="/logout" data-i18n data-en="logout" data-ru="выйти">logout</a>
  </span>
</nav>

{('<div class="flash">' + esc(flash) + '</div>') if flash and not flash.startswith('!') else ''}
{('<div class="flash err">' + esc(flash[1:]) + '</div>') if flash.startswith('!') else ''}

{body}

<script>
// Lang toggle — same pattern as landing page. Default EN, persisted in localStorage.
(function() {{
  const KEY = "aib_lang";
  const params = new URLSearchParams(location.search);
  const fromQuery = params.get("lang");
  const fromStore = localStorage.getItem(KEY);
  let lang = (fromQuery === "ru" || fromQuery === "en") ? fromQuery
            : (fromStore === "ru" || fromStore === "en") ? fromStore
            : "en";

  function apply(l) {{
    document.documentElement.lang = l;
    document.querySelectorAll("[data-i18n]").forEach(el => {{
      const txt = el.getAttribute("data-" + l);
      if (txt !== null) el.textContent = txt;
    }});
    document.querySelectorAll("input[data-en], input[data-ru]").forEach(el => {{
      const ph = el.getAttribute("data-" + l + "-placeholder");
      if (ph !== null) el.placeholder = ph;
    }});
    document.querySelectorAll("[data-en-placeholder], [data-ru-placeholder]").forEach(el => {{
      const ph = el.getAttribute("data-" + l + "-placeholder");
      if (ph !== null) el.placeholder = ph;
    }});
    document.querySelectorAll(".lang-toggle button").forEach(b => {{
      b.classList.toggle("active", b.dataset.lang === l);
    }});
    localStorage.setItem(KEY, l);
  }}
  document.querySelectorAll(".lang-toggle button").forEach(b => {{
    b.addEventListener("click", () => apply(b.dataset.lang));
  }});
  apply(lang);
}})();
</script>
<script>
// Provider → scope + models hint, driven by JSON inlined into the page.
(function() {{
  const metaEl = document.getElementById("provider-meta");
  const sel    = document.getElementById("add-key-provider");
  const scope  = document.getElementById("add-key-scope");
  const hint   = document.getElementById("provider-hint");
  if (!metaEl || !sel || !scope || !hint) return;
  const META = JSON.parse(metaEl.textContent);

  // Stash original hint so we can restore it
  const originalHint = hint.cloneNode(true);

  function langStrings() {{
    const l = document.documentElement.lang || "en";
    return l === "ru"
      ? {{ chosen: "Выбран:", scope: "scope:", models: "модели, которые будет вызывать брокер:", capabilities: "способности:" }}
      : {{ chosen: "Picked:",  scope: "scope:", models: "models the broker will route through this key:", capabilities: "capabilities:" }};
  }}

  function render(provider) {{
    const m = META[provider];
    if (!m) {{
      hint.replaceWith(originalHint.cloneNode(true));
      return;
    }}
    // Auto-set scope checkboxes: tick default; tick llm:edit too if this
    // provider is in the chat:edit chain so operator notices the option.
    const wantEdit = m.capabilities.includes("chat:edit");
    scope.querySelectorAll('input[type="checkbox"]').forEach(cb => {{
      cb.checked = (cb.value === m.default_scope) || (wantEdit && cb.value === "llm:edit");
    }});

    const t = langStrings();
    const capChips = m.capabilities.map(c =>
      '<span class="cap-tag">' + c + '</span>').join("");
    const modelRows = Object.entries(m.models).map(([cap, model]) =>
      '<tr><td>' + cap + '</td><td class="mono">' + model + '</td></tr>').join("");

    hint.innerHTML =
      '<div>' + t.chosen + ' <b>' + provider + '</b> · ' +
      t.scope + ' <code>' + m.default_scope + '</code></div>' +
      '<div style="margin-top:6px">' + t.capabilities + ' ' + capChips + '</div>' +
      '<div style="margin-top:8px">' + t.models + '</div>' +
      '<table>' + modelRows + '</table>';
  }}

  sel.addEventListener("change", () => render(sel.value));
  // Re-render on lang toggle so labels follow EN/RU
  document.querySelectorAll(".lang-toggle button").forEach(b => {{
    b.addEventListener("click", () => {{
      if (sel.value) setTimeout(() => render(sel.value), 0);
    }});
  }});
}})();
</script>
<script>
// Click-to-sort tables. <th class="sortable" data-type="num|text|date"> opt-in.
(function() {{
  function cellValue(tr, idx, kind) {{
    const td = tr.children[idx];
    const raw = (td.dataset.sort !== undefined) ? td.dataset.sort : td.textContent.trim();
    if (kind === "num") return parseFloat(raw.replace(/[$,]/g,"")) || 0;
    return raw.toLowerCase();
  }}
  document.querySelectorAll("th.sortable").forEach((th, idx) => {{
    const colIdx = Array.from(th.parentNode.children).indexOf(th);
    th.addEventListener("click", () => {{
      const table = th.closest("table");
      const tbody = table.tBodies[0];
      // Only sort .data-row tbody rows (skip inline edit-row)
      const rows = Array.from(tbody.querySelectorAll("tr.data-row"));
      const kind = th.dataset.type || "text";
      const asc = !th.classList.contains("asc");
      table.querySelectorAll("th.sortable").forEach(o => o.classList.remove("asc","desc"));
      th.classList.add(asc ? "asc" : "desc");
      rows.sort((a, b) => {{
        const va = cellValue(a, colIdx, kind);
        const vb = cellValue(b, colIdx, kind);
        if (va < vb) return asc ? -1 : 1;
        if (va > vb) return asc ?  1 : -1;
        return 0;
      }});
      rows.forEach(r => {{
        // Move data row + its edit-row partner together
        const partner = tbody.querySelector('tr.edit-row[data-edit-for="' + r.dataset.rowId + '"]');
        tbody.appendChild(r);
        if (partner) tbody.appendChild(partner);
      }});
    }});
  }});

  // Inline edit toggle
  document.querySelectorAll("button[data-edit-toggle]").forEach(btn => {{
    btn.addEventListener("click", () => {{
      const id = btn.dataset.editToggle;
      const editRow = document.querySelector('tr.edit-row[data-edit-for="' + id + '"]');
      const dataRow = document.querySelector('tr.data-row[data-row-id="' + id + '"]');
      if (!editRow) return;
      const wasActive = editRow.classList.contains("active");
      // Close any other open editors first
      document.querySelectorAll("tr.edit-row.active").forEach(r => {{
        r.classList.remove("active");
        const peer = document.querySelector('tr.data-row[data-row-id="' + r.dataset.editFor + '"]');
        if (peer) peer.classList.remove("editing");
      }});
      if (!wasActive) {{
        editRow.classList.add("active");
        dataRow.classList.add("editing");
      }}
    }});
  }});
}})();
</script>

</body></html>"""


def _parse_date_range(
    date_from: str | None, date_to: str | None
) -> tuple[date | None, date | None]:
    """Parse `from` and `to` strings (YYYY-MM-DD).

    Returns `(None, None)` when both inputs are missing — caller treats that
    as 'all-time, no date filter'. If only one side is given, the other
    defaults to today. Inverted ranges are swapped. Garbage strings fall back
    to today on that side (so a typo doesn't widen the window unexpectedly).
    """
    if not date_from and not date_to:
        return None, None
    today = datetime.now(UTC).date()
    try:
        df = date.fromisoformat(date_from) if date_from else today
    except ValueError:
        df = today
    try:
        dt = date.fromisoformat(date_to) if date_to else today
    except ValueError:
        dt = today
    if dt < df:
        df, dt = dt, df
    return df, dt


def _range_where(date_from: date | None, date_to: date | None) -> tuple[str, dict[str, Any]]:
    """Sargable half-open bounds on the bare `created_at` column — no
    `created_at::date` cast, so a plain btree index on `created_at` (migration
    005) can actually be used instead of a forced full scan."""
    if date_from is None and date_to is None:
        return "", {}
    start = datetime.combine(date_from, datetime.min.time()) if date_from else None
    end = (
        datetime.combine(date_to, datetime.min.time()) + timedelta(days=1)
        if date_to else None
    )
    if start is not None and end is not None:
        return "WHERE created_at >= :start AND created_at < :end", {"start": start, "end": end}
    if start is not None:
        return "WHERE created_at >= :start", {"start": start}
    return "WHERE created_at < :end", {"end": end}


async def _fetch_range_and_proj_spend(
    where_clause: str, bind_: dict[str, Any]
) -> tuple[dict[str, Any], dict[int, float]]:
    """ONE scan (GROUP BY project_id) replaces what used to be two separate
    full-table SUMs — the all-time/range grand total is the trivial in-Python
    sum of the handful of per-project rows (a few projects, not 451k log rows),
    and the per-project spend dict comes from the same rows."""
    async with get_session() as s:
        rows = (await s.execute(text(
            f"SELECT project_id, COUNT(*) AS calls, "
            f"       COALESCE(SUM(cost_usd),0) AS spend, "
            f"       COALESCE(SUM(tokens_in),0) AS tin, "
            f"       COALESCE(SUM(tokens_out),0) AS tout "
            f"FROM usage_log {where_clause} GROUP BY project_id"
        ), bind_)).all()
    totals = {
        "calls": sum(int(r.calls) for r in rows),
        "spend": sum(float(r.spend) for r in rows),
        "tin": sum(int(r.tin) for r in rows),
        "tout": sum(int(r.tout) for r in rows),
    }
    proj_spend = {int(r.project_id): float(r.spend) for r in rows if r.project_id is not None}
    return totals, proj_spend


async def _fetch_calls_1h() -> int:
    # Postgres-only `now()` — exercised by the Postgres-only integration tests
    # (test_gather_data_*), not the SQLite coverage run — hence the pragma.
    async with get_session() as s:  # pragma: no cover
        return int((await s.execute(text(
            "SELECT COUNT(*) FROM usage_log "
            "WHERE created_at > now() - interval '1 hour'"
        ))).scalar() or 0)


async def _fetch_tokens_today() -> dict[int, dict[str, int]]:
    """Per-key token consumption today (UTC) — drives the daily-quota bar.
    Split in/out so manual-override caps (e.g. corp Gemini 3M in / 80k out)
    can be tracked on each axis independently. Sargable bounds (computed in
    Python, not `created_at::date =`) so the created_at index applies."""
    today = datetime.now(UTC).date()
    start = datetime(today.year, today.month, today.day)
    end = start + timedelta(days=1)
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT api_key_id, "
            "  COALESCE(SUM(tokens_in + tokens_out), 0) AS tot, "
            "  COALESCE(SUM(tokens_in), 0)  AS tin, "
            "  COALESCE(SUM(tokens_out), 0) AS tout "
            "FROM usage_log "
            "WHERE api_key_id IS NOT NULL "
            "  AND created_at >= :start AND created_at < :end "
            "GROUP BY api_key_id"
        ), {"start": start, "end": end})).all()
    return {
        r.api_key_id: {"tot": int(r.tot), "tin": int(r.tin), "tout": int(r.tout)}
        for r in rows
    }


async def _fetch_provider_summary() -> list[Any]:
    # Postgres-only `now()`/`FILTER` — same pragma rationale as _fetch_calls_1h.
    # err_1h surfaces the 429/error storm per provider (was invisible without
    # digging the logs) — LEFT JOIN of last-hour non-ok calls, grouped by
    # provider.
    async with get_session() as s:  # pragma: no cover
        return list((await s.execute(text(
            "SELECT k.provider, "
            "COUNT(*) FILTER (WHERE is_active AND is_alive "
            "                  AND (cooldown_until IS NULL OR cooldown_until < now())) AS alive, "
            "COUNT(*) FILTER (WHERE NOT is_alive OR NOT is_active) AS dead, "
            "COUNT(*) AS total, "
            "COALESCE(e.err_1h, 0) AS err_1h "
            "FROM api_keys k "
            "LEFT JOIN ("
            "  SELECT provider, COUNT(*) AS err_1h FROM usage_log "
            "  WHERE status <> 'ok' AND created_at > now() - INTERVAL '1 hour' "
            "  GROUP BY provider"
            ") e ON e.provider = k.provider "
            "GROUP BY k.provider, e.err_1h ORDER BY k.provider"
        ))).all())


async def _fetch_projects() -> list[ProjectRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(ProjectRow).order_by(ProjectRow.id)
        )).scalars().all())


async def _fetch_keys() -> list[ApiKeyRow]:
    async with get_session() as s:
        return list((await s.execute(
            select(ApiKeyRow).order_by(ApiKeyRow.provider, ApiKeyRow.id)
        )).scalars().all())


async def _gather_data(date_from: date | None = None,
                        date_to: date | None = None) -> dict[str, Any]:
    # all-time when both None; date-clamped only when at least one is provided
    where_clause, bind_ = _range_where(date_from, date_to)

    # Six independent queries — none depends on another's result — so they run
    # concurrently, each on its own pooled connection (pool_size=10 +
    # max_overflow=20 comfortably covers this). A single AsyncSession can't run
    # concurrent statements, hence one get_session() per fetch, gathered here.
    (
        projects, keys, (range_totals, proj_spend),
        calls_1h, tokens_today, provider_summary,
    ) = await asyncio.gather(
        _fetch_projects(),
        _fetch_keys(),
        _fetch_range_and_proj_spend(where_clause, bind_),
        _fetch_calls_1h(),
        _fetch_tokens_today(),
        _fetch_provider_summary(),
    )
    return {
        "projects": projects, "keys": keys,
        "date_from": date_from, "date_to": date_to,
        "range_spend": float(range_totals["spend"]),
        "range_calls": int(range_totals["calls"]),
        "range_tin": int(range_totals["tin"]),
        "range_tout": int(range_totals["tout"]),
        "proj_spend": proj_spend,
        "tokens_today": tokens_today,
        "calls_1h": calls_1h,
        "provider_summary": provider_summary,
    }


def _render(data: dict[str, Any], *, flash: str = "",
             new_project_key: str | None = None) -> HTMLResponse:
    s = get_settings()

    df, dt = data["date_from"], data["date_to"]
    df_str = df.isoformat() if df else ""
    dt_str = dt.isoformat() if dt else ""
    all_time = df is None and dt is None
    if all_time:
        range_label_en = "all time"
        range_label_ru = "за всё время"
    elif df == dt:
        range_label_en = f"on {df_str}"
        range_label_ru = f"за {df_str}"
    else:
        range_label_en = f"{df_str or '…'} → {dt_str or '…'}"
        range_label_ru = f"{df_str or '…'} → {dt_str or '…'}"

    today_d = date.today()
    today_iso = today_d.isoformat()
    d7_iso = (today_d - timedelta(days=6)).isoformat()
    d30_iso = (today_d - timedelta(days=29)).isoformat()
    # Which quick-range link (if any) matches the current from/to — drives the
    # 'active' class below. Mutually exclusive by construction (different
    # from-dates), so at most one is ever true.
    is_today = df_str == today_iso and dt_str == today_iso
    is_7d = df_str == d7_iso and dt_str == today_iso
    is_30d = df_str == d30_iso and dt_str == today_iso
    range_form = f"""
    <form method="get" action="/dashboard" class="range-form">
      <label data-i18n data-en="From" data-ru="С">From</label>
      <input type="date" name="from" value="{df_str}" max="{today_iso}">
      <label data-i18n data-en="To" data-ru="по">To</label>
      <input type="date" name="to" value="{dt_str}" max="{today_iso}">
      <button type="submit" data-i18n data-en="apply" data-ru="применить">apply</button>
      <a href="/dashboard" class="range-reset{' active' if all_time else ''}" data-i18n
         data-en="all time" data-ru="за всё">all time</a>
      <span class="range-quick">
        <a href="?from={today_iso}&to={today_iso}" class="{'active' if is_today else ''}">today</a>
        <a href="?from={d7_iso}&to={today_iso}" class="{'active' if is_7d else ''}">7d</a>
        <a href="?from={d30_iso}&to={today_iso}" class="{'active' if is_30d else ''}">30d</a>
      </span>
    </form>"""

    cards = f"""
    {range_form}
    <div class="cards">
      <div class="card">
        <div class="card-label" data-i18n
             data-en="Spend ({range_label_en})" data-ru="Потрачено ({range_label_ru})">Spend ({range_label_en})</div>
        <div class="card-value">${data['range_spend']:.4f}</div>
        <div class="card-sub"><span data-i18n data-en="global cap" data-ru="общий лимит">global cap</span> ${s.GLOBAL_DAILY_CAP_USD}/day</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n
             data-en="Calls ({range_label_en})" data-ru="Вызовов ({range_label_ru})">Calls ({range_label_en})</div>
        <div class="card-value">{data['range_calls']:,}</div>
        <div class="card-sub">{data['calls_1h']} <span data-i18n data-en="in last 1h" data-ru="за последний час">in last 1h</span></div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Tokens in / out" data-ru="Токены вх / исх">Tokens in / out</div>
        <div class="card-value" style="font-size:18px">{data['range_tin']:,} / {data['range_tout']:,}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Projects · keys" data-ru="Проекты · ключи">Projects · keys</div>
        <div class="card-value">{len(data['projects'])} · {len(data['keys'])}</div>
      </div>
    </div>"""

    providers_html = "".join(
        f'<span class="provider"><b>{esc(p)}</b> '
        f'<span class="ok">{a}</span> / <span class="bad">{d}</span> / {t}'
        + (f' <span class="bad" title="errors in the last hour">⚠{e1h}/1h</span>'
           if e1h else "")
        + '</span>'
        for p, a, d, t, e1h in data["provider_summary"]
    )

    show_new_key = ""
    if new_project_key:
        show_new_key = (
            f'<div class="flash">Project created. SAVE this key now '
            f'(not retrievable later):<br><code>{esc(new_project_key)}</code></div>'
        )

    proj_spend = data["proj_spend"]
    rows_projects = ""
    projects_total_cap = 0.0
    projects_total_spend = 0.0
    for p in data["projects"]:
        scopes_csv = ",".join(p.allowed_scopes)
        cap_val = p.daily_cost_cap_usd if p.daily_cost_cap_usd is not None else ""
        cap_disp = f"${p.daily_cost_cap_usd:.2f}" if p.daily_cost_cap_usd is not None else "—"
        active_cell = "✓" if p.is_active else "✗"
        active_class = "ok" if p.is_active else "bad"
        p_spend = float(proj_spend.get(p.id, 0) or 0)
        projects_total_spend += p_spend
        if p.daily_cost_cap_usd is not None:
            projects_total_cap += float(p.daily_cost_cap_usd)
        rows_projects += (
            f'<tr class="data-row" data-row-id="p{p.id}">'
            f'<td class="rownum"></td>'
            f'<td data-sort="{p.id}">{p.id}</td>'
            f'<td><a href="/dashboard/projects/{p.id}" class="proj-link">'
            f'{esc(p.name)}</a></td>'
            f"<td><span class='pill'>{esc(scopes_csv)}</span></td>"
            f"<td class='{active_class}'>{active_cell}</td>"
            f"<td data-sort=\"{p.daily_cost_cap_usd or 0}\">{cap_disp}</td>"
            f"<td data-sort=\"{p_spend}\" class='mono'>${p_spend:.4f}</td>"
            f"<td><code>{esc(p.project_key_prefix)}…</code></td>"
            f'<td><button type="button" data-edit-toggle="p{p.id}" data-i18n '
            f'data-en="edit" data-ru="ред.">edit</button></td>'
            f"</tr>"
            # ── inline edit form row ──
            f'<tr class="edit-row" data-edit-for="p{p.id}"><td colspan="9">'
            f'<form method="post" action="/dashboard/projects/{p.id}/edit" class="row-form">'
            f'<input name="name" value="{esc(p.name)}" required>'
            f'<span class="scope-group">{_scope_checkboxes(p.allowed_scopes, "allowed_scopes")}</span>'
            f'<input name="daily_cost_cap_usd" type="number" step="0.01" '
            f'value="{cap_val}" '
            f'data-en-placeholder="cap (blank = none)" '
            f'data-ru-placeholder="лимит (пусто = нет)" '
            f'placeholder="cap (blank = none)">'
            f'<input name="owner_email" value="{esc(p.owner_email or "")}" '
            f'data-en-placeholder="owner email" '
            f'data-ru-placeholder="email владельца" '
            f'placeholder="owner email">'
            f'<button type="submit" data-i18n data-en="save" data-ru="сохранить">save</button>'
            f'<button type="button" data-edit-toggle="p{p.id}" data-i18n '
            f'data-en="cancel" data-ru="отмена">cancel</button>'
            f'</form></td></tr>'
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    rows_keys = ""
    keys_total_used = 0
    keys_total_spent = 0.0
    keys_total_cap = 0.0
    keys_total_errs = 0
    keys_alive = 0
    for k in data["keys"]:
        keys_total_used += k.daily_used or 0
        keys_total_spent += float(k.daily_cost_used_usd or 0)
        if k.daily_cost_cap_usd is not None:
            keys_total_cap += float(k.daily_cost_cap_usd)
        keys_total_errs += k.error_count or 0
        if k.is_alive and not (k.cooldown_until and k.cooldown_until > now):
            keys_alive += 1
        in_cd = k.cooldown_until and k.cooldown_until > now
        status_label = (
            "alive" if (k.is_alive and not in_cd)
            else "cooldown" if in_cd
            else "dead"
        )
        status_class = {"alive": "ok", "cooldown": "warn", "dead": "bad"}[status_label]
        status_ru = {"alive": "жив", "cooldown": "пауза", "dead": "мёртв"}[status_label]
        status_html = (
            f'<span class="{status_class}" data-i18n '
            f'data-en="{status_label}" data-ru="{status_ru}">{status_label}</span>'
        )

        # Daily-quota usage — show ALL capped axes (req / tok / in / out) so
        # it's obvious every key of a provider shares the SAME caps and only
        # the fill differs. Bar width/colour follows the dominant axis.
        # Token usage pulled live from usage_log (today UTC).
        tt = data["tokens_today"].get(k.id, {})
        tok_today = int(tt.get("tot", 0))
        tin_today = int(tt.get("tin", 0))
        tout_today = int(tt.get("tout", 0))
        axes = axes_for_key(
            k.daily_used or 0, tok_today, k,
            toks_in=tin_today, toks_out=tout_today,
        )
        if axes:
            used_pct = axes[0]["pct"]   # dominant axis drives bar + sort
            bar_fill = severity_class(used_pct)
            src = ("manual" if (k.manual_req_limit or k.manual_tok_limit
                                or k.manual_tok_in_limit or k.manual_tok_out_limit)
                   else "discovered" if k.limits_discovered_at else "default est.")
            # Compact per-axis chips: "84% tok · 15% req"
            chips = " · ".join(f"{a['pct']}% {a['short']}" for a in axes)
            # Tooltip spells out used/cap on every axis + source of the cap.
            detail = " · ".join(
                f"{a['used']:,}/{a['cap']:,} {a['short']}" for a in axes
            )
            used_html = (
                f"<span class='mono'>{chips}</span>"
                f"<span class='cap-bar' title='{detail} · {src}'>"
                f"<span class='fill {bar_fill}' style='width:{used_pct}%'></span></span>"
            )
        else:
            # paid / unknown — no quota, just the count
            used_pct = None
            used_html = f"<span class='mono'>{k.daily_used}</span>"
            # Sort paid keys after quota'd keys (use -1 sentinel in data-sort)

        used = float(k.daily_cost_used_usd or 0)
        cap_v = k.daily_cost_cap_usd
        if cap_v:
            pct = min(100, int(used / float(cap_v) * 100)) if cap_v else 0
            bar_cls = "fill bad" if pct >= 90 else "fill warn" if pct >= 70 else "fill"
            cap_html = (
                f"<span class='mono'>${used:.4f} / ${cap_v:.2f}</span>"
                f"<span class='cap-bar'><span class='{bar_cls}' "
                f"style='width:{pct}%'></span></span>"
            )
            cap_sort = float(cap_v)
        else:
            cap_html = f"<span class='mono'>${used:.4f}</span>"
            cap_sort = 0.0

        cap_input_val = f"{cap_v:.2f}" if cap_v is not None else ""
        scopes_csv = ",".join(k.scopes or ["llm:chat"])
        reserve_checked = " checked" if k.is_reserve else ""
        tier_options = "".join(
            f'<option value="{t}"{" selected" if t == k.tier else ""}>{t}</option>'
            for t in ("free", "paid", "trial")
        )

        rows_keys += (
            f'<tr class="data-row" data-row-id="k{k.id}">'
            f'<td class="rownum"></td>'
            f'<td data-sort="{k.id}">{k.id}</td>'
            f"<td>{esc(k.provider)}</td>"
            f"<td>{esc(k.label)}</td>"
            f"<td data-sort='{esc(k.tier)}'><span class='pill'>{esc(k.tier)}</span></td>"
            f"<td data-sort='{status_label}'>{status_html}</td>"
            f"<td data-sort='{used_pct if used_pct is not None else -1}'>{used_html}</td>"
            f"<td data-sort='{cap_sort}'>{cap_html}</td>"
            f"<td data-sort='{k.error_count}'>{k.error_count}</td>"
            f"<td>"
            f'<button type="button" data-edit-toggle="k{k.id}" '
            f'data-i18n data-en="edit" data-ru="ред.">edit</button> '
            f'<form class="inline" method="post" action="/dashboard/keys/{k.id}/disable">'
            f'<button type="submit" data-i18n '
            + (
                'data-en="enable" data-ru="вкл.">enable'
                if not k.is_active else
                'data-en="disable" data-ru="откл.">disable'
            )
            + "</button>"
            f'</form> '
            f'<form class="inline" method="post" action="/dashboard/keys/{k.id}/delete"'
            f' onsubmit="return confirm(\'Delete {esc(k.provider)}/{esc(k.label)}?\')">'
            f'<button class="danger" type="submit" data-i18n '
            f'data-en="del" data-ru="удал.">del</button>'
            f'</form>'
            f"</td></tr>"
            # ── inline edit form row ──
            f'<tr class="edit-row" data-edit-for="k{k.id}"><td colspan="10">'
            f'<form method="post" action="/dashboard/keys/{k.id}/edit" class="row-form">'
            f'<input name="label" value="{esc(k.label)}" required>'
            f'<select name="tier">{tier_options}</select>'
            f'<span class="scope-group">{_scope_checkboxes(k.scopes or ["llm:chat"])}</span>'
            f'<label class="rsv" title="reserved lane: picked last in its group, '
            f'invisible to other scopes"><input type="checkbox" name="is_reserve" '
            f'value="1"{reserve_checked}> reserve</label>'
            f'<input name="daily_cost_cap_usd" type="number" step="0.01" '
            f'value="{cap_input_val}" '
            f'data-en-placeholder="cap (blank = none)" '
            f'data-ru-placeholder="лимит (пусто = нет)" '
            f'placeholder="cap (blank = none)">'
            f'<input name="token" type="password" style="min-width:240px" '
            f'data-en-placeholder="new token (leave blank to keep)" '
            f'data-ru-placeholder="новый токен (пусто = оставить)" '
            f'placeholder="new token (leave blank to keep)">'
            f'<input name="account_id" value="{esc(k.account_id or "")}" style="min-width:150px" '
            f'title="Only needed for cloudflare." '
            f'data-en-placeholder="account ID" data-ru-placeholder="account ID" '
            f'placeholder="account ID">'
            f'<span class="quota-override" title="Manual daily quota override — '
            f'blank = use discovered/default. For corp keys (e.g. Gemini 3M in / 80k out).">'
            f'<input name="manual_req_limit" type="number" min="0" value="{k.manual_req_limit or ""}" '
            f'data-en-placeholder="req/day" data-ru-placeholder="запр/день" placeholder="req/day">'
            f'<input name="manual_tok_limit" type="number" min="0" value="{k.manual_tok_limit or ""}" '
            f'data-en-placeholder="tok/day" data-ru-placeholder="ток/день" placeholder="tok/day">'
            f'<input name="manual_tok_in_limit" type="number" min="0" value="{k.manual_tok_in_limit or ""}" '
            f'data-en-placeholder="in/day" data-ru-placeholder="вх/день" placeholder="in/day">'
            f'<input name="manual_tok_out_limit" type="number" min="0" value="{k.manual_tok_out_limit or ""}" '
            f'data-en-placeholder="out/day" data-ru-placeholder="исх/день" placeholder="out/day">'
            f'</span>'
            f'<button type="submit" data-i18n data-en="save" data-ru="сохранить">save</button>'
            f'<button type="button" data-edit-toggle="k{k.id}" data-i18n '
            f'data-en="cancel" data-ru="отмена">cancel</button>'
            f'</form></td></tr>'
        )

    provider_options = "".join(
        f'<option value="{p["provider"]}" data-scope="{p["default_scope"]}">'
        f'{p["provider"]}</option>'
        for p in _provider_catalogue()
    )

    add_key_form = f"""
    <fieldset><legend data-i18n data-en="Add API key" data-ru="Добавить API-ключ">Add API key</legend>
      <form method="post" action="/dashboard/keys/create" class="row-form" id="add-key-form">
        <select name="provider" id="add-key-provider" required>
          <option value="" disabled selected hidden
                  data-i18n data-en="— provider —" data-ru="— провайдер —">— provider —</option>
          {provider_options}
        </select>
        <input name="label" required
               data-en-placeholder="label (your handle, project, …)"
               data-ru-placeholder="ярлык (ваш handle, проект, …)"
               placeholder="label (your handle, project, …)">
        <input name="token" type="password" required style="min-width:280px"
               data-en-placeholder="raw token"
               data-ru-placeholder="токен"
               placeholder="raw token">
        <input name="account_id" style="min-width:180px"
               title="Only needed for cloudflare (its API URL embeds the account ID)."
               data-en-placeholder="account ID (cloudflare only)"
               data-ru-placeholder="account ID (только cloudflare)"
               placeholder="account ID (cloudflare only)">
        <select name="tier">
          <option value="free">free</option>
          <option value="paid">paid</option>
          <option value="trial">trial</option>
        </select>
        <span class="scope-group" id="add-key-scope">{_scope_checkboxes(["llm:chat"])}</span>
        <label class="rsv" title="reserved lane: picked last in its group, invisible to other scopes">
          <input type="checkbox" name="is_reserve" value="1"> reserve</label>
        <input name="daily_cost_cap_usd" type="number" step="0.01" style="min-width:130px"
               data-en-placeholder="$ cap (optional)"
               data-ru-placeholder="$ лимит (опц.)"
               placeholder="$ cap (optional)">
        <span class="quota-override" title="Optional daily quota override — blank = use discovered/default. For known caps (e.g. corp Gemini 3M in / 80k out).">
          <input name="manual_req_limit" type="number" min="0"
                 data-en-placeholder="req/day" data-ru-placeholder="запр/день" placeholder="req/day">
          <input name="manual_tok_limit" type="number" min="0"
                 data-en-placeholder="tok/day" data-ru-placeholder="ток/день" placeholder="tok/day">
          <input name="manual_tok_in_limit" type="number" min="0"
                 data-en-placeholder="in/day" data-ru-placeholder="вх/день" placeholder="in/day">
          <input name="manual_tok_out_limit" type="number" min="0"
                 data-en-placeholder="out/day" data-ru-placeholder="исх/день" placeholder="out/day">
        </span>
        <button type="submit" data-i18n data-en="add" data-ru="добавить">add</button>
      </form>
      <div id="provider-hint" class="provider-hint"
           data-i18n
           data-en="Pick a provider — the form will set the right scope and show which models the broker will route through this key."
           data-ru="Выберите провайдера — форма проставит нужный scope и покажет, какие модели брокер будет вызывать через этот ключ.">
        Pick a provider — the form will set the right scope and show which models the broker will route through this key.
      </div>
    </fieldset>"""

    add_project_form = f"""
    <fieldset><legend data-i18n data-en="Add project" data-ru="Добавить проект">Add project</legend>
      <form method="post" action="/dashboard/projects/create" class="row-form">
        <input name="name" required
               data-en-placeholder="name (lowercase, e.g. stepan)"
               data-ru-placeholder="имя (lowercase, напр. stepan)"
               placeholder="name (lowercase, e.g. stepan)">
        <input name="owner_email"
               data-en-placeholder="owner email"
               data-ru-placeholder="email владельца"
               placeholder="owner email">
        <span class="scope-group">{_scope_checkboxes(["llm:chat", "llm:embed"], "allowed_scopes")}</span>
        <input name="daily_cost_cap_usd" type="number" step="0.01"
               data-en-placeholder="cap (optional)"
               data-ru-placeholder="лимит (опц.)"
               placeholder="cap (optional)">
        <button type="submit" data-i18n data-en="create" data-ru="создать">create</button>
      </form>
    </fieldset>"""

    body = f"""
    <script id="provider-meta" type="application/json">{_provider_meta_json()}</script>
    {show_new_key}
    {cards}

    <h2 data-i18n data-en="Providers" data-ru="Провайдеры">Providers</h2>
    <div>{providers_html or '<span class="provider">none</span>'}</div>

    <h2 data-i18n data-en="Projects" data-ru="Проекты">Projects</h2>
    {add_project_form}
    <table><thead><tr>
      <th>#</th>
      <th class="sortable" data-type="num" data-i18n data-en="id" data-ru="id">id</th>
      <th class="sortable" data-i18n data-en="name" data-ru="имя">name</th>
      <th class="sortable" data-i18n data-en="scopes" data-ru="права">scopes</th>
      <th class="sortable" data-i18n data-en="act" data-ru="акт">act</th>
      <th class="sortable" data-type="num" data-i18n data-en="daily cap" data-ru="суточный лимит">daily cap</th>
      <th class="sortable" data-type="num" data-i18n
          data-en="spend in range" data-ru="потрачено за период">spend in range</th>
      <th class="sortable" data-i18n data-en="key prefix" data-ru="префикс ключа">key prefix</th>
      <th data-i18n data-en="actions" data-ru="действия">actions</th>
    </tr></thead><tbody>{rows_projects}</tbody>
    <tfoot><tr>
      <td colspan="4" class="k" data-i18n data-en="TOTAL" data-ru="ИТОГО">TOTAL</td>
      <td>{len(data['projects'])}</td>
      <td class="num">${projects_total_cap:.2f}</td>
      <td class="num">${projects_total_spend:.4f}</td>
      <td colspan="2"></td>
    </tr></tfoot>
    </table>

    <h2 data-i18n data-en="API keys" data-ru="API-ключи">API keys</h2>
    {add_key_form}
    <table><thead><tr>
      <th>#</th>
      <th class="sortable" data-type="num" data-i18n data-en="id" data-ru="id">id</th>
      <th class="sortable" data-i18n data-en="provider" data-ru="провайдер">provider</th>
      <th class="sortable" data-i18n data-en="label" data-ru="ярлык">label</th>
      <th class="sortable" data-i18n data-en="tier" data-ru="тариф">tier</th>
      <th class="sortable" data-i18n data-en="status" data-ru="статус">status</th>
      <th class="sortable" data-type="num" data-i18n
          data-en="daily %" data-ru="% дня">daily %</th>
      <th class="sortable" data-type="num" data-i18n data-en="$/cap" data-ru="$/лимит">$/cap</th>
      <th class="sortable" data-type="num" data-i18n data-en="errs" data-ru="ошибки">errs</th>
      <th data-i18n data-en="actions" data-ru="действия">actions</th>
    </tr></thead><tbody>{rows_keys}</tbody>
    <tfoot><tr>
      <td colspan="5" class="k" data-i18n data-en="TOTAL" data-ru="ИТОГО">TOTAL</td>
      <td><span data-i18n data-en="{keys_alive} alive" data-ru="{keys_alive} живых">{keys_alive} alive</span> / {len(data['keys'])}</td>
      <td class="num">{keys_total_used:,}</td>
      <td class="num">${keys_total_spent:.4f} / ${keys_total_cap:.2f}</td>
      <td class="num">{keys_total_errs}</td>
      <td></td>
    </tr></tfoot>
    </table>
    """
    return HTMLResponse(_dash_html(body=body, flash=flash), headers=_NO_STORE)


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    flash: str = "",
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
) -> HTMLResponse:
    try:
        require_owner_session(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    df, dt = _parse_date_range(from_, to)
    data = await _gather_data(df, dt)
    return _render(data, flash=flash)


# ─── Project drill-down ─────────────────────────────────────────────────────

_RANGE_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}

# Latency histogram: fixed edges (ms). width_bucket(x, edges) → 0..len(edges),
# giving len(edges)+1 buckets that must line up 1:1 with the labels below.
_LAT_EDGES_MS = (250, 500, 1000, 2000, 5000, 10000, 30000)
_LAT_LABELS = ("<250ms", "250-500ms", "0.5-1s", "1-2s",
               "2-5s", "5-10s", "10-30s", ">30s")
_LAT_SQL_ARRAY = "ARRAY[" + ",".join(str(e) for e in _LAT_EDGES_MS) + "]"


def _lat_hist_counts(rows: list[Any]) -> list[int]:
    """Map sparse (bucket, count) rows from width_bucket into a dense per-label
    count list (empty buckets → 0)."""
    counts = [0] * len(_LAT_LABELS)
    for r in rows:
        b = int(r.b)
        if 0 <= b < len(counts):
            counts[b] = int(r.n)
    return counts


async def _gather_project_detail(project_id: int, hours: int) -> dict[str, Any] | None:
    """Pull aggregates + recent calls for one project over the last `hours`."""
    async with get_session() as s:
        project = await s.get(ProjectRow, project_id)
        if not project:
            return None
        bind_ = {"pid": project_id, "h": hours}
        totals = (await s.execute(text(
            "SELECT COUNT(*) AS calls, "
            "       COALESCE(SUM(cost_usd),0) AS spend, "
            "       COALESCE(SUM(tokens_in),0) AS tin, "
            "       COALESCE(SUM(tokens_out),0) AS tout, "
            "       COALESCE(SUM(cache_read_tokens),0) AS cache_read, "
            "       COALESCE(SUM(cache_write_tokens),0) AS cache_write, "
            "       COALESCE(AVG(latency_ms),0) AS avg_lat, "
            "       COUNT(*) FILTER (WHERE status='ok') AS ok_n, "
            "       COUNT(*) FILTER (WHERE status<>'ok') AS err_n "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour')"
        ), bind_)).one()
        by_provider = (await s.execute(text(
            "SELECT provider, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS spend "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY provider ORDER BY n DESC"
        ), bind_)).all()
        by_model = (await s.execute(text(
            "SELECT model, COUNT(*) AS n, COALESCE(SUM(cost_usd),0) AS spend, "
            "       COALESCE(SUM(tokens_in+tokens_out),0) AS toks "
            "FROM usage_log WHERE project_id=:pid AND model IS NOT NULL "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY model ORDER BY n DESC LIMIT 12"
        ), bind_)).all()
        by_capability = (await s.execute(text(
            "SELECT COALESCE(capability,'(none)') AS cap, COUNT(*) AS n, "
            "       COALESCE(SUM(cost_usd),0) AS spend "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY cap ORDER BY n DESC"
        ), bind_)).all()
        by_workflow = (await s.execute(text(
            "SELECT COALESCE(workflow,'(none)') AS wf, COUNT(*) AS n, "
            "       COALESCE(SUM(cost_usd),0) AS spend "
            "FROM usage_log WHERE project_id=:pid "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY wf ORDER BY spend DESC, n DESC"
        ), bind_)).all()
        lat_hist_rows = (await s.execute(text(
            f"SELECT width_bucket(latency_ms, {_LAT_SQL_ARRAY}) AS b, "
            "       COUNT(*) AS n "
            "FROM usage_log WHERE project_id=:pid AND latency_ms IS NOT NULL "
            "  AND created_at > now() - (:h * INTERVAL '1 hour') "
            "GROUP BY b ORDER BY b"
        ), bind_)).all()
        recent = (await s.execute(text(
            "SELECT id, created_at, provider, model, capability, "
            "       tokens_in, tokens_out, cost_usd, latency_ms, status, "
            "       http_status, error_kind "
            "FROM usage_log WHERE project_id=:pid "
            "ORDER BY created_at DESC LIMIT 50"
        ), {"pid": project_id})).all()
        # Active key count by scope intersection (informational)
    return {
        "project": project,
        "hours": hours,
        "totals": totals,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_capability": by_capability,
        "by_workflow": by_workflow,
        "lat_hist": _lat_hist_counts(list(lat_hist_rows)),
        "recent": recent,
    }


def _cache_card(cache_read: int, cache_write: int) -> str:
    """Prompt-cache KPI card — only anthropic calls ever populate these
    (apply_prompt_cache), so most projects/ranges show neither; omit the card
    entirely rather than show a permanent 0/0. Reuse ratio (reads per write)
    is the honest cache-efficiency signal: one write feeds many cheap reads."""
    if not cache_read and not cache_write:
        return ""
    reuse = f"{cache_read / cache_write:.1f}× reuse" if cache_write else "—"
    return f"""
      <div class="card">
        <div class="card-label" data-i18n data-en="Prompt cache" data-ru="Кэш промпта">Prompt cache</div>
        <div class="card-value" style="font-size:18px">{cache_read:,} / {cache_write:,}</div>
        <div class="card-sub" data-i18n
             data-en="read / write · {reuse}" data-ru="чтения / записи · {reuse}">read / write · {reuse}</div>
      </div>
    """


def _render_project_detail(d: dict[str, Any]) -> HTMLResponse:
    p = d["project"]
    t = d["totals"]
    err_pct = (t.err_n / t.calls * 100.0) if t.calls else 0.0
    ok_pct  = (t.ok_n  / t.calls * 100.0) if t.calls else 0.0

    cap_disp = (
        f"${p.daily_cost_cap_usd:.2f}" if p.daily_cost_cap_usd is not None else "—"
    )

    range_links = "".join(
        f'<a href="?range={r}" class="{"active" if d["hours"] == h else ""}">{r}</a>'
        for r, h in _RANGE_HOURS.items()
    )

    cards = f"""
    <div class="cards">
      <div class="card">
        <div class="card-label" data-i18n data-en="Calls" data-ru="Вызовов">Calls</div>
        <div class="card-value">{t.calls}</div>
        <div class="card-sub">
          <span class="ok">{t.ok_n} ok</span> ·
          <span class="bad">{t.err_n} err</span> ({err_pct:.0f}%)
        </div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Spend" data-ru="Потрачено">Spend</div>
        <div class="card-value">${float(t.spend):.4f}</div>
        <div class="card-sub" data-i18n
             data-en="daily cap {cap_disp}" data-ru="суточный лимит {cap_disp}">daily cap {cap_disp}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Tokens in / out" data-ru="Токены вх / исх">Tokens in / out</div>
        <div class="card-value" style="font-size:18px">{t.tin:,} / {t.tout:,}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Avg latency" data-ru="Средн. задержка">Avg latency</div>
        <div class="card-value">{int(t.avg_lat or 0)} ms</div>
        <div class="card-sub">{ok_pct:.0f}% success</div>
      </div>
      {_cache_card(t.cache_read, t.cache_write)}
    </div>
    """

    def _bd_card(title_en: str, title_ru: str, rows: list[tuple],
                  fmt_row, total_label_en: str = "total",
                  total_label_ru: str = "итого", total: tuple | None = None) -> str:
        body = "".join(fmt_row(r) for r in rows) or (
            '<tr><td colspan="3" style="color:#5a6171" data-i18n '
            'data-en="(no data in this range)" data-ru="(нет данных за период)">'
            "(no data in this range)</td></tr>"
        )
        total_html = ""
        if total:
            total_html = (
                '<tr class="total-row">'
                f'<td data-i18n data-en="{total_label_en}" data-ru="{total_label_ru}">'
                f'{total_label_en}</td>'
                f'<td class="num">{total[0]}</td><td class="num">{total[1]}</td>'
                '</tr>'
            )
        return (
            f'<div class="brk-card">'
            f'<h3 data-i18n data-en="{title_en}" data-ru="{title_ru}">{title_en}</h3>'
            f'<table><tbody>{body}{total_html}</tbody></table></div>'
        )

    prov_card = _bd_card("By provider", "По провайдерам", list(d["by_provider"]),
        lambda r: f'<tr><td class="k">{esc(r.provider)}</td>'
                  f'<td class="num">{r.n}</td>'
                  f'<td class="num">${float(r.spend):.4f}</td></tr>',
        total=(t.calls, f"${float(t.spend):.4f}"))

    cap_card = _bd_card("By capability", "По способностям",
        list(d["by_capability"]),
        lambda r: f'<tr><td class="k">{esc(r.cap)}</td>'
                  f'<td class="num">{r.n}</td>'
                  f'<td class="num">${float(r.spend):.4f}</td></tr>')

    wf_card = _bd_card("By workflow", "По workflow",
        list(d["by_workflow"]),
        lambda r: f'<tr><td class="k">{esc(r.wf)}</td>'
                  f'<td class="num">{r.n}</td>'
                  f'<td class="num">${float(r.spend):.4f}</td></tr>')

    model_card = _bd_card("Top models", "Топ моделей", list(d["by_model"]),
        lambda r: f'<tr><td class="k" style="font-size:11px">{esc(r.model or "")}</td>'
                  f'<td class="num">{r.n}</td>'
                  f'<td class="num">${float(r.spend):.4f}</td></tr>')

    # Latency histogram: count of calls per latency bucket (same period), bars
    # scaled to the busiest bucket. Reuses the cap-bar/fill quota-bar styling.
    lat_counts = d["lat_hist"]
    lat_max = max(lat_counts) or 1
    lat_rows = "".join(
        f'<tr><td class="k">{esc(lbl)}</td>'
        f"<td style='width:55%'><span class='cap-bar'>"
        f"<span class='fill' style='width:{int(n / lat_max * 100)}%'></span>"
        f'</span></td><td class="num">{n}</td></tr>'
        for lbl, n in zip(_LAT_LABELS, lat_counts, strict=True)
    )
    lat_card = (
        '<div class="brk-card">'
        '<h3 data-i18n data-en="Latency distribution" '
        'data-ru="Распределение задержек">Latency distribution</h3>'
        f'<table><tbody>{lat_rows}</tbody></table></div>'
    ) if sum(lat_counts) else ""

    # tr.data-row marker is required by the sortable-table JS in _dash_html.
    # data-sort on the time column uses iso8601 so lexical sort works.
    # data-row-id is usage_log.id — the same request_id returned to the API
    # caller in its response, so the caller can paste it here to find the call.
    recent_rows = "".join(
        f'<tr class="data-row" data-row-id="{r.id}">'
        f'<td class="num" data-sort="{r.id}" '
        f'style="color:#666;font-size:11px">{r.id}</td>'
        f'<td data-sort="{r.created_at.isoformat()}" '
        f'style="color:#888;font-size:11px">'
        f'{r.created_at.strftime("%m-%d %H:%M:%S")}</td>'
        f'<td>{esc(r.provider)}</td>'
        f'<td style="color:#888;font-size:11px">{esc((r.model or "—")[:32])}</td>'
        f'<td><span class="pill">{esc(r.capability or "—")}</span></td>'
        f'<td class="num" data-sort="{r.tokens_in + r.tokens_out}">'
        f'{r.tokens_in}/{r.tokens_out}</td>'
        f'<td class="num" data-sort="{float(r.cost_usd)}">'
        f'${float(r.cost_usd):.4f}</td>'
        f'<td class="num" data-sort="{r.latency_ms or 0}">'
        f'{r.latency_ms or "—"}</td>'
        f'<td class="status-{esc(r.status)}">{esc(r.status)}</td>'
        f'<td style="color:#888;font-size:11px">{r.http_status or ""} '
        f'{esc(r.error_kind or "")}</td></tr>'
        for r in d["recent"]
    ) or '<tr><td colspan="10" style="color:#5a6171">no calls yet</td></tr>'

    body = f"""
    <div class="breadcrumb">
      <a href="/dashboard">← dashboard</a> /
      <span>project {p.id}</span>
    </div>
    <h1 style="margin:0 0 4px;font-weight:500">{esc(p.name)}
      <span class="range-pills">{range_links}</span>
    </h1>
    <div style="color:#888;font-size:13px;margin-bottom:18px">
      <code>{esc(p.project_key_prefix)}…</code> ·
      scopes <span class="pill">{esc(",".join(p.allowed_scopes))}</span> ·
      {"active" if p.is_active else "<span class=bad>disabled</span>"}
      {f" · owner {esc(p.owner_email)}" if p.owner_email else ""}
    </div>

    {cards}

    <div class="breakdown">
      {prov_card}
      {cap_card}
      {wf_card}
      {model_card}
      {lat_card}
    </div>

    <h2 data-i18n data-en="Recent 50 calls" data-ru="Последние 50 вызовов">Recent 50 calls</h2>
    <table class="recent-table"><thead><tr>
      <th class="sortable" data-type="num"
          title="usage_log.id — the same request_id returned in the API response"
          data-i18n data-en="req id" data-ru="req id">req id</th>
      <th class="sortable">when</th>
      <th class="sortable">provider</th>
      <th class="sortable">model</th>
      <th class="sortable">cap</th>
      <th class="sortable" data-type="num">tok in/out</th>
      <th class="sortable" data-type="num">$</th>
      <th class="sortable" data-type="num">ms</th>
      <th class="sortable">status</th>
      <th>http / err</th>
    </tr></thead><tbody>{recent_rows}</tbody></table>
    """
    return HTMLResponse(_dash_html(body=body), headers=_NO_STORE)


@router.get("/dashboard/projects/{project_id}", response_class=HTMLResponse)
async def dashboard_project_detail(
    project_id: int,
    request: Request,
    range: str = "24h",
) -> HTMLResponse:
    try:
        require_owner_session(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    hours = _RANGE_HOURS.get(range, 24)
    d = await _gather_project_detail(project_id, hours)
    if d is None:
        return RedirectResponse(
            "/dashboard?flash=!Project+not+found", status_code=303
        )
    return _render_project_detail(d)


# ─── Form handlers ──────────────────────────────────────────────────────────


_KNOWN_SCOPES = ("llm:chat", "llm:embed", "llm:vision", "llm:edit", "llm:deep")


def _parse_scopes(csv: str) -> list[str] | None:
    """Parse a comma-separated scope list; None if empty or any scope unknown.
    Kept for legacy callers (project allowed_scopes form is still CSV)."""
    scopes = [x.strip() for x in csv.split(",") if x.strip()]
    if not scopes or any(s not in _KNOWN_SCOPES for s in scopes):
        return None
    return scopes


def _validate_scope_list(scopes: list[str]) -> list[str] | None:
    """For checkbox-driven forms — strip dups, reject empty / unknown."""
    seen: list[str] = []
    for s in scopes:
        s = s.strip()
        if not s:
            continue
        if s not in _KNOWN_SCOPES:
            return None
        if s not in seen:
            seen.append(s)
    return seen or None


def _positive_int_or_none(v: str) -> int | None:
    """Parse an optional positive-int form field. Blank/garbage/≤0 → None
    (no manual override on that axis). Used by both add- and edit-key forms."""
    v = (v or "").strip()
    if not v:
        return None
    try:
        n = int(v)
    except ValueError:
        return None
    return n if n > 0 else None


def _apply_manual_limits(key: ApiKeyRow, *, req: str, tok: str,
                         tok_in: str, tok_out: str) -> None:
    """Set the four manual daily-quota overrides on a key from raw form
    strings (parsed via _positive_int_or_none). Shared by add-create, upsert
    and edit so the four axes stay in lock-step everywhere."""
    key.manual_req_limit = _positive_int_or_none(req)
    key.manual_tok_limit = _positive_int_or_none(tok)
    key.manual_tok_in_limit = _positive_int_or_none(tok_in)
    key.manual_tok_out_limit = _positive_int_or_none(tok_out)


def _scope_checkboxes(selected: list[str] | None, name: str = "scopes") -> str:
    """Render the 4 known scopes as checkboxes (multi-select via repeated POST)."""
    sel = set(selected or [])
    return "".join(
        f'<label class="scope-cb">'
        f'<input type="checkbox" name="{name}" value="{s}"'
        f'{" checked" if s in sel else ""}> {s}'
        f'</label>'
        for s in _KNOWN_SCOPES
    )


@router.post("/dashboard/keys/create")
async def dash_create_key(
    request: Request,
    provider: str = Form(...),
    label: str = Form(...),
    token: str = Form(...),
    tier: str = Form("free"),
    scopes: Annotated[list[str] | None, Form()] = None,
    is_reserve: bool = Form(False),
    daily_cost_cap_usd: str = Form(""),
    manual_req_limit: str = Form(""),
    manual_tok_limit: str = Form(""),
    manual_tok_in_limit: str = Form(""),
    manual_tok_out_limit: str = Form(""),
    account_id: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    scope_list = _validate_scope_list(scopes or [])
    if scope_list is None:
        return RedirectResponse("/dashboard?flash=!Bad+or+empty+scope", status_code=303)
    cap = float(daily_cost_cap_usd) if daily_cost_cap_usd.strip() else None
    account_id_val = account_id.strip() or None  # pragma: no cover
    # Parsing is unit-tested via _apply_manual_limits / _positive_int_or_none;
    # the DB-write glue below only runs on Postgres (SQLite can't autoincrement
    # the BigInteger PK), so it's exercised by the Postgres-only integration
    # test test_create_and_edit_key_persist_manual_limits, not the SQLite
    # coverage run — hence the pragmas.
    limits = {"req": manual_req_limit, "tok": manual_tok_limit,  # pragma: no cover
              "tok_in": manual_tok_in_limit, "tok_out": manual_tok_out_limit}
    new_id: int | None = None
    async with get_session() as s:
        existing = (await s.execute(
            select(ApiKeyRow).where(
                ApiKeyRow.provider == provider, ApiKeyRow.label == label
            )
        )).scalar_one_or_none()
        if existing:
            existing.token_encrypted = encrypt(token)
            existing.tier = tier
            existing.scopes = scope_list
            existing.is_reserve = is_reserve
            existing.daily_cost_cap_usd = cap
            existing.account_id = account_id_val  # pragma: no cover
            _apply_manual_limits(existing, **limits)  # pragma: no cover
            existing.is_active = True
            existing.is_alive = True
            verb = "updated"
            new_id = existing.id
        else:
            fresh = ApiKeyRow(
                provider=provider, label=label, tier=tier,
                scopes=scope_list, is_reserve=is_reserve,
                token_encrypted=encrypt(token),
                daily_cost_cap_usd=cap,
                account_id=account_id_val,
            )
            _apply_manual_limits(fresh, **limits)  # pragma: no cover
            s.add(fresh)
            await s.flush()
            new_id = fresh.id
            verb = "added"
    await audit(actor="dashboard", action=f"key.{verb}",
                target=f"{provider}/{label}",
                metadata={"scopes": scope_list, "is_reserve": is_reserve,
                          "manual_limits": {k: _positive_int_or_none(v)
                                            for k, v in limits.items()}},
                ip=_ip(request))
    # Auto-discover free-tier limits from response headers (best-effort).
    if new_id is not None:
        with contextlib.suppress(Exception):
            await discover_and_store(new_id, provider, token)
    return RedirectResponse(
        f"/dashboard?flash=Key+{provider}/{label}+{verb}", status_code=303
    )


@router.post("/dashboard/keys/{key_id}/disable")
async def dash_toggle_key(
    key_id: int, request: Request,
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            return RedirectResponse("/dashboard?flash=!Key+not+found", status_code=303)
        row.is_active = not row.is_active
        if row.is_active:
            row.is_alive = True   # give it another chance
            row.error_count = 0
        state = "enabled" if row.is_active else "disabled"
    await audit(actor="dashboard", action=f"key.{state}", target=f"id={key_id}",
                ip=_ip(request))
    return RedirectResponse(
        f"/dashboard?flash=Key+id={key_id}+{state}", status_code=303
    )


@router.post("/dashboard/keys/{key_id}/delete")
async def dash_delete_key(
    key_id: int, request: Request,
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            return RedirectResponse("/dashboard?flash=!Key+not+found", status_code=303)
        target = f"{row.provider}/{row.label}"
        await s.delete(row)
    await audit(actor="dashboard", action="key.delete", target=target, ip=_ip(request))
    return RedirectResponse(
        f"/dashboard?flash=Key+{target}+deleted", status_code=303
    )


@router.post("/dashboard/keys/{key_id}/edit")
async def dash_edit_key(
    key_id: int,
    request: Request,
    label: str = Form(...),
    tier: str = Form("free"),
    scopes: Annotated[list[str] | None, Form()] = None,
    is_reserve: bool = Form(False),
    daily_cost_cap_usd: str = Form(""),
    token: str = Form(""),
    account_id: str = Form(""),
    manual_req_limit: str = Form(""),
    manual_tok_limit: str = Form(""),
    manual_tok_in_limit: str = Form(""),
    manual_tok_out_limit: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    if tier not in ("free", "paid", "trial"):
        return RedirectResponse("/dashboard?flash=!Bad+tier", status_code=303)
    scope_list = _validate_scope_list(scopes or [])
    if scope_list is None:
        return RedirectResponse("/dashboard?flash=!Bad+or+empty+scope", status_code=303)
    cap_v = float(daily_cost_cap_usd) if daily_cost_cap_usd.strip() else None

    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            return RedirectResponse("/dashboard?flash=!Key+not+found", status_code=303)
        row.label = label
        row.tier = tier
        row.scopes = scope_list
        row.is_reserve = is_reserve
        row.daily_cost_cap_usd = cap_v
        row.account_id = account_id.strip() or None  # pragma: no cover
        _apply_manual_limits(  # pragma: no cover
            row, req=manual_req_limit, tok=manual_tok_limit,
            tok_in=manual_tok_in_limit, tok_out=manual_tok_out_limit)
        if token.strip():
            row.token_encrypted = encrypt(token.strip())
        target = f"{row.provider}/{row.label}"
    await audit(actor="dashboard", action="key.edit", target=target,
                metadata={"tier": tier, "scopes": scope_list, "is_reserve": is_reserve,
                          "cap": cap_v, "token_rotated": bool(token.strip()),
                          "manual_req": row.manual_req_limit,
                          "manual_tok": row.manual_tok_limit,
                          "manual_tok_in": row.manual_tok_in_limit,
                          "manual_tok_out": row.manual_tok_out_limit},
                ip=_ip(request))
    return RedirectResponse(
        f"/dashboard?flash=Key+{target}+updated", status_code=303
    )


@router.post("/dashboard/projects/{project_id}/edit")
async def dash_edit_project(
    project_id: int,
    request: Request,
    name: str = Form(...),
    allowed_scopes: Annotated[list[str] | None, Form()] = None,
    daily_cost_cap_usd: str = Form(""),
    owner_email: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    scopes = _validate_scope_list(allowed_scopes or [])
    if scopes is None:
        return RedirectResponse("/dashboard?flash=!Bad+or+empty+scope", status_code=303)
    cap_v = float(daily_cost_cap_usd) if daily_cost_cap_usd.strip() else None
    async with get_session() as s:
        row = await s.get(ProjectRow, project_id)
        if not row:
            return RedirectResponse("/dashboard?flash=!Project+not+found", status_code=303)
        row.name = name
        row.allowed_scopes = scopes
        row.daily_cost_cap_usd = cap_v
        row.owner_email = owner_email or None
    await audit(actor="dashboard", action="project.edit", target=name,
                metadata={"scopes": scopes, "cap": cap_v}, ip=_ip(request))
    return RedirectResponse(
        f"/dashboard?flash=Project+{name}+updated", status_code=303
    )


@router.post("/dashboard/projects/create", response_class=HTMLResponse)
async def dash_create_project(
    request: Request,
    name: str = Form(...),
    owner_email: str = Form(""),
    allowed_scopes: Annotated[list[str] | None, Form()] = None,
    daily_cost_cap_usd: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> HTMLResponse:
    scopes = _validate_scope_list(allowed_scopes or ["llm:chat", "llm:embed"])
    if scopes is None:
        return RedirectResponse("/dashboard?flash=!Bad+or+empty+scope", status_code=303)
    from aibroker.auth import generate_project_key, hash_project_key
    plain = generate_project_key()
    h = hash_project_key(plain)
    cap = float(daily_cost_cap_usd) if daily_cost_cap_usd.strip() else None
    async with get_session() as s:
        row = ProjectRow(
            name=name, owner_email=owner_email or None,
            project_key_hash=h, project_key_prefix=plain[:12],
            allowed_scopes=scopes, daily_cost_cap_usd=cap,
        )
        s.add(row)
    await audit(actor="dashboard", action="project.create", target=name,
                metadata={"scopes": scopes}, ip=_ip(request))
    data = await _gather_data()
    return _render(data, new_project_key=plain,
                    flash=f"Project {name} created.")


def _ip(req: Request) -> str | None:
    return req.client.host if req.client else None
