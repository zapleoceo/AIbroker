"""Browser admin UI — Telegram login, dashboard, inline forms for CRUD."""
from __future__ import annotations

import contextlib
from html import escape as esc
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

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
from aibroker.routes.dashboard_assets import (
    _DASHBOARD_CSS,
    _DASHBOARD_JS,
    _LOGIN_HTML,
    _NO_STORE,
)
from aibroker.routes.dashboard_data import (
    _RANGE_HOURS,
    _gather_data,
    _gather_project_detail,
    _parse_date_range,
)
from aibroker.routes.dashboard_render import (
    _render,
    _render_project_detail,
)
from aibroker.routes.dashboard_scopes import (
    _validate_scope_list,
)
from aibroker.telemetry import audit

router = APIRouter(tags=["dashboard"])


# ─── Login ──────────────────────────────────────────────────────────────────


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


# Static shell (CSS/JS) never changes per-request — only the data (keys,
# projects, usage tables) does. Serving it inline with the same no-store
# headers as the data meant every dashboard navigation re-downloaded and
# re-parsed the same ~17KB of markup. Split out to versioned (?v=__version__)
# long-cached assets; the HTML document itself stays no-store (see _NO_STORE)
# so admin data is always fresh. No auth on these two routes — pure styling/
# behavior, zero user data, and letting Cloudflare's edge cache them too is a
# feature, not a risk.
_LONG_CACHE = {"Cache-Control": "public, max-age=31536000, immutable"}


@router.get("/dashboard/assets.css")
async def dashboard_assets_css() -> Response:
    return Response(_DASHBOARD_CSS, media_type="text/css", headers=_LONG_CACHE)


@router.get("/dashboard/assets.js")
async def dashboard_assets_js() -> Response:
    return Response(_DASHBOARD_JS, media_type="application/javascript", headers=_LONG_CACHE)


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
