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
          text-align:center; border:1px solid #2a2d34; }}
  h1 {{ font-weight:500; font-size:28px; margin:0 0 8px; }}
  p {{ color:#888; margin:6px 0 24px; font-size:14px; }}
  .err {{ color:#f44336; margin-top:18px; font-size:13px; }}
</style></head><body>
<div class="box">
  <h1>AIbroker</h1>
  <p>Sign in with Telegram to administer</p>
  <script async src="https://telegram.org/js/telegram-widget.js?22"
          data-telegram-login="__BOT__"
          data-size="large"
          data-radius="8"
          data-auth-url="https://__HOST__/api/tg_login"
          data-request-access="write"></script>
  __ERR__
</div>
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
</style></head><body>

<nav>
  <h1>AIbroker</h1>
  <span class="pill">v0.1.0</span>
  <span class="right">
    <a href="/v1/health">/v1/health</a>
    <a href="/docs">/docs</a>
    <a href="/logout">logout</a>
  </span>
</nav>

{('<div class="flash">' + esc(flash) + '</div>') if flash and not flash.startswith('!') else ''}
{('<div class="flash err">' + esc(flash[1:]) + '</div>') if flash.startswith('!') else ''}

{body}

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
      <div class="card"><div class="card-label">Spend today</div>
        <div class="card-value">${data['spend_today']:.4f}</div>
        <div class="card-sub">cap ${s.GLOBAL_DAILY_CAP_USD}</div></div>
      <div class="card"><div class="card-label">Calls 1h</div>
        <div class="card-value">{data['calls_1h']}</div></div>
      <div class="card"><div class="card-label">Projects</div>
        <div class="card-value">{len(data['projects'])}</div></div>
      <div class="card"><div class="card-label">API keys</div>
        <div class="card-value">{len(data['keys'])}</div></div>
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

    rows_projects = "".join(
        f"<tr><td>{p.id}</td><td>{esc(p.name)}</td>"
        f"<td><span class='pill'>{', '.join(esc(s) for s in p.allowed_scopes)}</span></td>"
        f"<td>{'<span class=ok>✓</span>' if p.is_active else '<span class=bad>✗</span>'}</td>"
        f"<td>{p.daily_cost_cap_usd if p.daily_cost_cap_usd is not None else '—'}</td>"
        f"<td><code>{esc(p.project_key_prefix)}…</code></td></tr>"
        for p in data["projects"]
    )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows_keys = ""
    for k in data["keys"]:
        in_cd = k.cooldown_until and k.cooldown_until > now
        status = (
            '<span class="ok">alive</span>' if (k.is_alive and not in_cd)
            else '<span class="warn">cooldown</span>' if in_cd
            else '<span class="bad">dead</span>'
        )
        cap = (f"${k.daily_cost_used_usd:.4f}/${k.daily_cost_cap_usd}"
               if k.daily_cost_cap_usd else f"${k.daily_cost_used_usd:.4f}")
        rows_keys += (
            f"<tr><td>{k.id}</td><td>{esc(k.provider)}</td><td>{esc(k.label)}</td>"
            f"<td><span class='pill'>{esc(k.tier)}</span></td>"
            f"<td>{status}</td><td>{k.daily_used}</td><td class='mono'>{cap}</td>"
            f"<td>{k.error_count}</td>"
            f"<td>"
            f'  <form class="inline" method="post" action="/dashboard/keys/{k.id}/disable">'
            f'    <button type="submit">{"enable" if not k.is_active else "disable"}</button>'
            f'  </form> '
            f'  <form class="inline" method="post" action="/dashboard/keys/{k.id}/delete"'
            f'        onsubmit="return confirm(\'Delete {esc(k.provider)}/{esc(k.label)}?\')">'
            f'    <button class="danger" type="submit">delete</button>'
            f'  </form>'
            f"</td></tr>"
        )

    add_key_form = """
    <fieldset><legend>Add API key</legend>
      <form method="post" action="/dashboard/keys/create" class="row-form">
        <input name="provider" placeholder="provider (cerebras, …)" required>
        <input name="label" placeholder="label (demoniwwwe, …)" required>
        <input name="token" type="password" placeholder="raw token" required style="min-width:280px">
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
        <input name="daily_cost_cap_usd" type="number" step="0.01" placeholder="cap (optional)" style="min-width:130px">
        <button type="submit">add</button>
      </form>
    </fieldset>"""

    add_project_form = """
    <fieldset><legend>Add project</legend>
      <form method="post" action="/dashboard/projects/create" class="row-form">
        <input name="name" placeholder="name (lowercase, e.g. stepan)" required>
        <input name="owner_email" placeholder="owner email">
        <input name="allowed_scopes" value="llm:chat,llm:embed"
               placeholder="scopes comma-sep" style="min-width:240px">
        <input name="daily_cost_cap_usd" type="number" step="0.01" placeholder="cap (optional)">
        <button type="submit">create</button>
      </form>
    </fieldset>"""

    body = f"""
    {show_new_key}
    {cards}

    <h2>Providers</h2>
    <div>{providers_html or '<span class="provider">none</span>'}</div>

    <h2>Projects</h2>
    {add_project_form}
    <table><thead><tr><th>id</th><th>name</th><th>scopes</th><th>act</th>
    <th>daily cap</th><th>key prefix</th></tr></thead><tbody>{rows_projects}</tbody></table>

    <h2>API keys</h2>
    {add_key_form}
    <table><thead><tr><th>id</th><th>provider</th><th>label</th><th>tier</th>
    <th>status</th><th>used</th><th>$/cap</th><th>errs</th><th>actions</th>
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
