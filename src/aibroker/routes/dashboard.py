"""Browser admin UI — Telegram login, dashboard, inline forms for CRUD."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape as esc
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select, text, update

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
from aibroker.telemetry import audit

router = APIRouter(tags=["dashboard"])


# ─── Login ──────────────────────────────────────────────────────────────────


_LOGIN_HTML = """<!doctype html><html><head>
<meta charset="utf-8"><title>AIbroker — login</title>
<style>
  body {{ font-family:-apple-system, sans-serif; background:#0f1115; color:#e4e6eb;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }}
  .box {{ background:#1a1d24; padding:48px 40px; border-radius:14px; max-width:420px;
          text-align:center; border:1px solid #2a2d34; position:relative; }}
  h1 {{ font-weight:500; font-size:28px; margin:0 0 8px; }}
  p {{ color:#888; margin:6px 0 24px; font-size:14px; }}
  .err {{ color:#f44336; margin-top:18px; font-size:13px; }}
  .lang-toggle {{ position:absolute; top:14px; right:14px;
                display:inline-flex; background:#0f1115; border:1px solid #2a2d34;
                border-radius:6px; overflow:hidden;
                font-family:ui-monospace,monospace; font-size:11px; }}
  .lang-toggle button {{ background:none; border:none; color:#888;
                       padding:5px 10px; cursor:pointer;
                       font-family:ui-monospace,monospace; font-size:11px; }}
  .lang-toggle button.active {{ background:rgba(77,171,247,.12); color:#4dabf7; }}
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
</body></html>"""


@router.get("/login", response_class=HTMLResponse)
async def login_page(error: str | None = None) -> HTMLResponse:
    s = get_settings()
    bot = s.TELEGRAM_BOT_USERNAME or "telegram"
    err_html = f'<div class="err">{esc(error)}</div>' if error else ""
    return HTMLResponse(
        _LOGIN_HTML.replace("__BOT__", bot)
                   .replace("__HOST__", s.PUBLIC_HOST)
                   .replace("__ERR__", err_html)
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
/* Cap bar */
.cap-bar {{ display:inline-block; width:80px; height:6px; background:#0f1115;
            border-radius:3px; vertical-align:middle; margin-left:6px; overflow:hidden; }}
.cap-bar .fill {{ display:block; height:100%; background:#4dabf7; }}
.cap-bar .fill.warn {{ background:#ffd84a; }}
.cap-bar .fill.bad  {{ background:#f44336; }}
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


async def _gather_data() -> dict[str, Any]:
    async with get_session() as s:
        projects = (await s.execute(
            select(ProjectRow).order_by(ProjectRow.id)
        )).scalars().all()
        keys = (await s.execute(
            select(ApiKeyRow).order_by(ApiKeyRow.provider, ApiKeyRow.id)
        )).scalars().all()
        today = datetime.now(timezone.utc).date()
        spend_today = float((await s.execute(
            text("SELECT COALESCE(SUM(cost_usd), 0) FROM usage_log "
                 "WHERE created_at::date = :d"),
            {"d": today},
        )).scalar() or 0.0)
        calls_1h = int((await s.execute(
            text("SELECT COUNT(*) FROM usage_log "
                 "WHERE created_at > now() - interval '1 hour'")
        )).scalar() or 0)
        provider_summary = (await s.execute(text(
            "SELECT provider, "
            "COUNT(*) FILTER (WHERE is_active AND is_alive "
            "                  AND (cooldown_until IS NULL OR cooldown_until < now())) AS alive, "
            "COUNT(*) FILTER (WHERE NOT is_alive OR NOT is_active) AS dead, "
            "COUNT(*) AS total FROM api_keys GROUP BY provider ORDER BY provider"
        ))).all()
    return {
        "projects": projects, "keys": keys,
        "spend_today": spend_today, "calls_1h": calls_1h,
        "provider_summary": provider_summary,
    }


def _render(data: dict[str, Any], *, flash: str = "",
             new_project_key: str | None = None) -> HTMLResponse:
    s = get_settings()

    cards = f"""
    <div class="cards">
      <div class="card">
        <div class="card-label" data-i18n data-en="Spend today" data-ru="Сегодня потрачено">Spend today</div>
        <div class="card-value">${data['spend_today']:.4f}</div>
        <div class="card-sub"><span data-i18n data-en="cap" data-ru="лимит">cap</span> ${s.GLOBAL_DAILY_CAP_USD}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Calls 1h" data-ru="Вызовов за час">Calls 1h</div>
        <div class="card-value">{data['calls_1h']}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="Projects" data-ru="Проекты">Projects</div>
        <div class="card-value">{len(data['projects'])}</div>
      </div>
      <div class="card">
        <div class="card-label" data-i18n data-en="API keys" data-ru="API-ключи">API keys</div>
        <div class="card-value">{len(data['keys'])}</div>
      </div>
    </div>"""

    providers_html = "".join(
        f'<span class="provider"><b>{esc(p)}</b> '
        f'<span class="ok">{a}</span> / <span class="bad">{d}</span> / {t}</span>'
        for p, a, d, t in data["provider_summary"]
    )

    show_new_key = ""
    if new_project_key:
        show_new_key = (
            f'<div class="flash">Project created. SAVE this key now '
            f'(not retrievable later):<br><code>{esc(new_project_key)}</code></div>'
        )

    rows_projects = ""
    for p in data["projects"]:
        scopes_csv = ",".join(p.allowed_scopes)
        cap_val = p.daily_cost_cap_usd if p.daily_cost_cap_usd is not None else ""
        cap_disp = f"${p.daily_cost_cap_usd:.2f}" if p.daily_cost_cap_usd is not None else "—"
        active_cell = "✓" if p.is_active else "✗"
        active_class = "ok" if p.is_active else "bad"
        rows_projects += (
            f'<tr class="data-row" data-row-id="p{p.id}">'
            f'<td data-sort="{p.id}">{p.id}</td>'
            f'<td>{esc(p.name)}</td>'
            f"<td><span class='pill'>{esc(scopes_csv)}</span></td>"
            f"<td class='{active_class}'>{active_cell}</td>"
            f"<td data-sort=\"{p.daily_cost_cap_usd or 0}\">{cap_disp}</td>"
            f"<td><code>{esc(p.project_key_prefix)}…</code></td>"
            f'<td><button type="button" data-edit-toggle="p{p.id}" data-i18n '
            f'data-en="edit" data-ru="ред.">edit</button></td>'
            f"</tr>"
            # ── inline edit form row ──
            f'<tr class="edit-row" data-edit-for="p{p.id}"><td colspan="7">'
            f'<form method="post" action="/dashboard/projects/{p.id}/edit" class="row-form">'
            f'<input name="name" value="{esc(p.name)}" required>'
            f'<input name="allowed_scopes" value="{esc(scopes_csv)}" style="min-width:240px">'
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

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows_keys = ""
    for k in data["keys"]:
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
        scope_now = (k.scopes[0] if k.scopes else "llm:chat")
        scope_options = "".join(
            f'<option value="{s}"{" selected" if s == scope_now else ""}>{s}</option>'
            for s in ("llm:chat", "llm:embed", "llm:vision")
        )
        tier_options = "".join(
            f'<option value="{t}"{" selected" if t == k.tier else ""}>{t}</option>'
            for t in ("free", "paid", "trial")
        )

        rows_keys += (
            f'<tr class="data-row" data-row-id="k{k.id}">'
            f'<td data-sort="{k.id}">{k.id}</td>'
            f"<td>{esc(k.provider)}</td>"
            f"<td>{esc(k.label)}</td>"
            f"<td data-sort='{esc(k.tier)}'><span class='pill'>{esc(k.tier)}</span></td>"
            f"<td data-sort='{status_label}'>{status_html}</td>"
            f"<td data-sort='{k.daily_used}'>{k.daily_used}</td>"
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
            f'<tr class="edit-row" data-edit-for="k{k.id}"><td colspan="9">'
            f'<form method="post" action="/dashboard/keys/{k.id}/edit" class="row-form">'
            f'<input name="label" value="{esc(k.label)}" required>'
            f'<select name="tier">{tier_options}</select>'
            f'<select name="scope">{scope_options}</select>'
            f'<input name="daily_cost_cap_usd" type="number" step="0.01" '
            f'value="{cap_input_val}" '
            f'data-en-placeholder="cap (blank = none)" '
            f'data-ru-placeholder="лимит (пусто = нет)" '
            f'placeholder="cap (blank = none)">'
            f'<input name="token" type="password" style="min-width:240px" '
            f'data-en-placeholder="new token (leave blank to keep)" '
            f'data-ru-placeholder="новый токен (пусто = оставить)" '
            f'placeholder="new token (leave blank to keep)">'
            f'<button type="submit" data-i18n data-en="save" data-ru="сохранить">save</button>'
            f'<button type="button" data-edit-toggle="k{k.id}" data-i18n '
            f'data-en="cancel" data-ru="отмена">cancel</button>'
            f'</form></td></tr>'
        )

    add_key_form = """
    <fieldset><legend data-i18n data-en="Add API key" data-ru="Добавить API-ключ">Add API key</legend>
      <form method="post" action="/dashboard/keys/create" class="row-form">
        <input name="provider" required
               data-en-placeholder="provider (cerebras, …)"
               data-ru-placeholder="провайдер (cerebras, …)"
               placeholder="provider (cerebras, …)">
        <input name="label" required
               data-en-placeholder="label"
               data-ru-placeholder="ярлык"
               placeholder="label">
        <input name="token" type="password" required style="min-width:280px"
               data-en-placeholder="raw token"
               data-ru-placeholder="токен"
               placeholder="raw token">
        <select name="tier">
          <option value="free">free</option>
          <option value="paid">paid</option>
          <option value="trial">trial</option>
        </select>
        <select name="scope">
          <option value="llm:chat">llm:chat</option>
          <option value="llm:embed">llm:embed</option>
          <option value="llm:vision">llm:vision</option>
        </select>
        <input name="daily_cost_cap_usd" type="number" step="0.01" style="min-width:130px"
               data-en-placeholder="cap (optional)"
               data-ru-placeholder="лимит (опц.)"
               placeholder="cap (optional)">
        <button type="submit" data-i18n data-en="add" data-ru="добавить">add</button>
      </form>
    </fieldset>"""

    add_project_form = """
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
        <input name="allowed_scopes" value="llm:chat,llm:embed" style="min-width:240px"
               data-en-placeholder="scopes comma-sep"
               data-ru-placeholder="права через запятую"
               placeholder="scopes comma-sep">
        <input name="daily_cost_cap_usd" type="number" step="0.01"
               data-en-placeholder="cap (optional)"
               data-ru-placeholder="лимит (опц.)"
               placeholder="cap (optional)">
        <button type="submit" data-i18n data-en="create" data-ru="создать">create</button>
      </form>
    </fieldset>"""

    body = f"""
    {show_new_key}
    {cards}

    <h2 data-i18n data-en="Providers" data-ru="Провайдеры">Providers</h2>
    <div>{providers_html or '<span class="provider">none</span>'}</div>

    <h2 data-i18n data-en="Projects" data-ru="Проекты">Projects</h2>
    {add_project_form}
    <table><thead><tr>
      <th class="sortable" data-type="num" data-i18n data-en="id" data-ru="id">id</th>
      <th class="sortable" data-i18n data-en="name" data-ru="имя">name</th>
      <th class="sortable" data-i18n data-en="scopes" data-ru="права">scopes</th>
      <th class="sortable" data-i18n data-en="act" data-ru="акт">act</th>
      <th class="sortable" data-type="num" data-i18n data-en="daily cap" data-ru="суточный лимит">daily cap</th>
      <th class="sortable" data-i18n data-en="key prefix" data-ru="префикс ключа">key prefix</th>
      <th data-i18n data-en="actions" data-ru="действия">actions</th>
    </tr></thead><tbody>{rows_projects}</tbody></table>

    <h2 data-i18n data-en="API keys" data-ru="API-ключи">API keys</h2>
    {add_key_form}
    <table><thead><tr>
      <th class="sortable" data-type="num" data-i18n data-en="id" data-ru="id">id</th>
      <th class="sortable" data-i18n data-en="provider" data-ru="провайдер">provider</th>
      <th class="sortable" data-i18n data-en="label" data-ru="ярлык">label</th>
      <th class="sortable" data-i18n data-en="tier" data-ru="тариф">tier</th>
      <th class="sortable" data-i18n data-en="status" data-ru="статус">status</th>
      <th class="sortable" data-type="num" data-i18n data-en="used" data-ru="исп.">used</th>
      <th class="sortable" data-type="num" data-i18n data-en="$/cap" data-ru="$/лимит">$/cap</th>
      <th class="sortable" data-type="num" data-i18n data-en="errs" data-ru="ошибки">errs</th>
      <th data-i18n data-en="actions" data-ru="действия">actions</th>
    </tr></thead><tbody>{rows_keys}</tbody></table>
    """
    return HTMLResponse(_dash_html(body=body, flash=flash))


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    flash: str = "",
) -> HTMLResponse:
    try:
        require_owner_session(request)
    except HTTPException:
        return RedirectResponse("/login", status_code=303)
    data = await _gather_data()
    return _render(data, flash=flash)


# ─── Form handlers ──────────────────────────────────────────────────────────


@router.post("/dashboard/keys/create")
async def dash_create_key(
    request: Request,
    provider: str = Form(...),
    label: str = Form(...),
    token: str = Form(...),
    tier: str = Form("free"),
    scope: str = Form("llm:chat"),
    daily_cost_cap_usd: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    cap = float(daily_cost_cap_usd) if daily_cost_cap_usd.strip() else None
    async with get_session() as s:
        existing = (await s.execute(
            select(ApiKeyRow).where(
                ApiKeyRow.provider == provider, ApiKeyRow.label == label
            )
        )).scalar_one_or_none()
        if existing:
            existing.token_encrypted = encrypt(token)
            existing.tier = tier
            existing.scopes = [scope]
            existing.daily_cost_cap_usd = cap
            existing.is_active = True
            existing.is_alive = True
            verb = "updated"
        else:
            s.add(ApiKeyRow(
                provider=provider, label=label, tier=tier,
                scopes=[scope], token_encrypted=encrypt(token),
                daily_cost_cap_usd=cap,
            ))
            verb = "added"
    await audit(actor="dashboard", action=f"key.{verb}",
                target=f"{provider}/{label}", ip=_ip(request))
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
    scope: str = Form("llm:chat"),
    daily_cost_cap_usd: str = Form(""),
    token: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    if tier not in ("free", "paid", "trial"):
        return RedirectResponse("/dashboard?flash=!Bad+tier", status_code=303)
    if scope not in ("llm:chat", "llm:embed", "llm:vision"):
        return RedirectResponse("/dashboard?flash=!Bad+scope", status_code=303)
    cap_v = float(daily_cost_cap_usd) if daily_cost_cap_usd.strip() else None
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            return RedirectResponse("/dashboard?flash=!Key+not+found", status_code=303)
        row.label = label
        row.tier = tier
        row.scopes = [scope]
        row.daily_cost_cap_usd = cap_v
        if token.strip():
            row.token_encrypted = encrypt(token.strip())
        target = f"{row.provider}/{row.label}"
    await audit(actor="dashboard", action="key.edit", target=target,
                metadata={"tier": tier, "scope": scope, "cap": cap_v,
                          "token_rotated": bool(token.strip())},
                ip=_ip(request))
    return RedirectResponse(
        f"/dashboard?flash=Key+{target}+updated", status_code=303
    )


@router.post("/dashboard/projects/{project_id}/edit")
async def dash_edit_project(
    project_id: int,
    request: Request,
    name: str = Form(...),
    allowed_scopes: str = Form(""),
    daily_cost_cap_usd: str = Form(""),
    owner_email: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    scopes = [x.strip() for x in allowed_scopes.split(",") if x.strip()]
    if not scopes:
        return RedirectResponse("/dashboard?flash=!Need+at+least+one+scope", status_code=303)
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
    allowed_scopes: str = Form("llm:chat,llm:embed"),
    daily_cost_cap_usd: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> HTMLResponse:
    from aibroker.auth import generate_project_key, hash_project_key
    plain = generate_project_key()
    h = hash_project_key(plain)
    scopes = [x.strip() for x in allowed_scopes.split(",") if x.strip()]
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
