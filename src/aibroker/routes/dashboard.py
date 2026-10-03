"""Browser admin UI — Telegram login, static assets and the state-changing form
handlers (create / edit / delete / rotate). The read-only pages live in
dashboard_pages.py; their templates in aibroker/web/templates.
"""
from __future__ import annotations

import contextlib
import math
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from aibroker.auth import client_ip, generate_project_key, hash_project_key
from aibroker.auth_session import (
    COOKIE_NAME,
    OwnerSession,
    issue_session_cookie,
    require_owner_session,
    verify_telegram_widget,
)
from aibroker.config import get_settings
from aibroker.crypto import decrypt, encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.providers.auto_discover import discover_and_store
from aibroker.providers.health_probes import probe
from aibroker.routes import dashboard_pages
from aibroker.routes.dashboard_assets import _LONG_CACHE
from aibroker.routes.dashboard_scopes import (
    _is_known_provider,
    _validate_scope_list,
)
from aibroker.routing.chains import usable_scopes_for_provider
from aibroker.telemetry import audit
from aibroker.web.render import STATIC_DIR, render, render_html

# include_in_schema=False (2026-10-02): /openapi.json is public (the landing
# page links it) and was advertising every owner-only route, form field
# included. The client API stays in the schema.
router = APIRouter(tags=["dashboard"], include_in_schema=False)
router.include_router(dashboard_pages.router)


# ─── Login ──────────────────────────────────────────────────────────────────


@router.get("/login", response_class=HTMLResponse)
async def login_page(error: str | None = None) -> HTMLResponse:
    s = get_settings()
    return render("login.html", bot=s.TELEGRAM_BOT_USERNAME or "telegram",
                  host=s.PUBLIC_HOST, error=error)


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
    await audit(actor=f"tg:{user_id}", action="login.success", ip=client_ip(request))
    return resp


@router.get("/logout")
async def logout_get() -> RedirectResponse:
    """GET must not log out (2026-10-02): any page could embed <img src=/logout>
    and sign the owner out. The nav button POSTs; a stray GET just lands on the
    dashboard."""
    return RedirectResponse("/dashboard", status_code=303)


@router.post("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ─── Static assets ──────────────────────────────────────────────────────────

# CSS/JS/vendored libraries are real files under aibroker/web/static, served
# long-cached and versioned (?v=<content hash>, see dashboard_assets). No auth:
# pure styling/behavior, zero user data, and the edge may cache them.
_MEDIA = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml",
          ".json": "application/json", ".map": "application/json", ".woff2": "font/woff2"}


@router.get("/dashboard/static/{path:path}")
async def dashboard_static(path: str) -> Response:
    root = STATIC_DIR.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(Path(target), media_type=_MEDIA.get(target.suffix, "application/octet-stream"),
                        headers=_LONG_CACHE)


# ─── Form handlers ──────────────────────────────────────────────────────────


_MAX_NAME_LEN = 100  # api_keys.label / projects.name display + the admin API's max_length


def _flash_url(msg: str, base: str = "/dashboard") -> str:
    """Redirect target with `msg` URL-encoded. Names were interpolated raw, so a
    label with `&`/`#`/`%` truncated or corrupted the flash and could inject
    extra query parameters (2026-10-02 review). `!` prefix = error."""
    return f"{base}{'&' if '?' in base else '?'}flash=" + quote_plus(msg)


# Forms say where to land via a hidden `next`; anything but a plain dashboard
# path (optionally ?tab=x) falls back to /dashboard, so it is not an open redirect.
_NEXT_RE = re.compile(r"^/dashboard(?:/[a-z0-9_-]+)*(?:\?tab=[a-z]+)?$")


def _safe_next(next_: str) -> str:
    return next_ if _NEXT_RE.match(next_ or "") else "/dashboard"


def _back(next_: str, msg: str) -> RedirectResponse:
    return RedirectResponse(_flash_url(msg, _safe_next(next_)), status_code=303)


def _parse_request_cap(v: str) -> int | None:
    """Blank -> None (unlimited lifetime requests); otherwise an integer >= 0
    (0 = block every request). Junk / negative / fractional raise ValueError."""
    v = (v or "").strip()
    if not v:
        return None
    n = int(v)
    if n < 0:
        raise ValueError("request cap must be >= 0")
    return n


def _parse_cost_cap(v: str) -> float | None:
    """Blank -> None (no cap). Junk, negative, nan and inf raise ValueError:
    float() accepted "nan"/"inf" (a NaN cap never trips `>=`, i.e. no cap at all)
    and a bare float("abc") was an unhandled 500 (2026-10-02 review)."""
    v = (v or "").strip()
    if not v:
        return None
    cap = float(v)
    if not math.isfinite(cap) or cap < 0:
        raise ValueError("cap must be a finite number >= 0")
    return cap


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
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    if not _is_known_provider(provider):
        return _back(next, "!Unknown provider")
    if not usable_scopes_for_provider(provider):
        # In no routing chain (mistral since 2026-09-12): the key could never be
        # picked, and its scope boxes are all disabled, so it could not be edited.
        return _back(next, f"!{provider} is in no routing chain, key not added")
    if len(label) > _MAX_NAME_LEN:
        return _back(next, "!Label too long (max 100)")
    scope_list = _validate_scope_list(scopes or [])
    if scope_list is None:
        return _back(next, "!Bad or empty scope")
    try:
        cap = _parse_cost_cap(daily_cost_cap_usd)
    except ValueError:
        return _back(next, "!Bad cost cap")
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
                ip=client_ip(request))
    # Auto-discover free-tier limits from response headers (best-effort).
    if new_id is not None:
        with contextlib.suppress(Exception):
            await discover_and_store(new_id, provider, token)
    return _back(next, f"Key {provider}/{label} {verb}")


@router.post("/dashboard/keys/{key_id}/disable")
async def dash_toggle_key(
    key_id: int, request: Request,
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            return _back(next, "!Key not found")
        row.is_active = not row.is_active
        if row.is_active:
            row.is_alive = True   # give it another chance
            row.error_count = 0
        state = "enabled" if row.is_active else "disabled"
        target = f"{row.provider}/{row.label}"
    await audit(actor="dashboard", action=f"key.{state}", target=f"id={key_id}",
                ip=client_ip(request))
    return _back(next, f"Key {target} {state}")


@router.post("/dashboard/keys/{key_id}/delete")
async def dash_delete_key(
    key_id: int, request: Request,
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            return _back(next, "!Key not found")
        target = f"{row.provider}/{row.label}"
        await s.delete(row)
    await audit(actor="dashboard", action="key.delete", target=target, ip=client_ip(request))
    return _back(next, f"Key {target} deleted")


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
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    if tier not in ("free", "paid", "trial"):
        return _back(next, "!Bad tier")
    if len(label) > _MAX_NAME_LEN:
        return _back(next, "!Label too long (max 100)")
    scope_list = _validate_scope_list(scopes or [])
    if scope_list is None:
        # A key whose provider is in no chain has every scope box disabled (a
        # disabled box is not submitted), so an empty list is expected there
        # and the key keeps its scopes (below). Anything else is a bad form.
        async with get_session() as s:
            probe = await s.get(ApiKeyRow, key_id)
        if probe is None or usable_scopes_for_provider(probe.provider):
            return _back(next, "!Bad or empty scope")
    try:
        cap_v = _parse_cost_cap(daily_cost_cap_usd)
    except ValueError:
        return _back(next, "!Bad cost cap")

    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            return _back(next, "!Key not found")
        if not usable_scopes_for_provider(row.provider):
            scope_list = list(row.scopes or [])   # nothing editable: keep as stored
        elif scope_list is None:
            return _back(next, "!Bad or empty scope")
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
                ip=client_ip(request))
    return _back(next, f"Key {target} updated")


@router.post("/dashboard/projects/{project_id}/edit")
async def dash_edit_project(
    project_id: int,
    request: Request,
    name: str = Form(...),
    allowed_scopes: Annotated[list[str] | None, Form()] = None,
    daily_cost_cap_usd: str = Form(""),
    total_request_cap: str = Form(""),
    req_cap_present: str = Form(""),
    owner_email: str = Form(""),
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    # total_request_cap is only applied when the form says it carried the field
    # (`req_cap_present`; FastAPI turns a blank Form value into "absent", so blank
    # alone cannot mean "unlimited"). A client that does not know the field can
    # therefore never silently lift a self-signup cap.
    if len(name) > _MAX_NAME_LEN:
        return _back(next, "!Name too long (max 100)")
    scopes = _validate_scope_list(allowed_scopes or [])
    if scopes is None:
        return _back(next, "!Bad or empty scope")
    try:
        cap_v = _parse_cost_cap(daily_cost_cap_usd)
    except ValueError:
        return _back(next, "!Bad cost cap")
    try:
        req_cap = _parse_request_cap(total_request_cap) if req_cap_present else None
    except ValueError:
        return _back(next, "!Bad request cap (whole number >= 0, blank = unlimited)")
    async with get_session() as s:
        row = await s.get(ProjectRow, project_id)
        if not row:
            return _back(next, "!Project not found")
        row.name = name
        row.allowed_scopes = scopes
        row.daily_cost_cap_usd = cap_v
        if req_cap_present:
            row.total_request_cap = req_cap
        row.owner_email = owner_email or None
    await audit(actor="dashboard", action="project.edit", target=name,
                metadata={"scopes": scopes, "cap": cap_v,
                          "total_request_cap": req_cap if req_cap_present else "unchanged"},
                ip=client_ip(request))
    return _back(next, f"Project {name} updated")


@router.post("/dashboard/projects/{project_id}/delete")
async def dash_delete_project(
    project_id: int, request: Request,
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> RedirectResponse:
    """Hard-delete a client project (2026-09-12, owner request — the panel
    had no way to remove a client). Its key stops authenticating at once.
    usage_log rows keep their project_id (history and billing stay
    auditable; the per-project drill-down 404s). deep_jobs rows go WITH the
    project (FK ondelete=CASCADE, db/models.py): a pending job vanishes and
    its poller gets 404; a job already mid-flight finishes into the void
    (_execute's "project no longer exists" branch then updates 0 rows)."""
    async with get_session() as s:
        row = await s.get(ProjectRow, project_id)
        if not row:
            return _back(next, "!Project not found")
        target = row.name
        await s.delete(row)
    await audit(actor="dashboard", action="project.delete", target=target,
                ip=client_ip(request))
    return _back(next, f"Project {target} deleted")


@router.post("/dashboard/projects/create", response_class=HTMLResponse)
async def dash_create_project(
    request: Request,
    name: str = Form(...),
    owner_email: str = Form(""),
    allowed_scopes: Annotated[list[str] | None, Form()] = None,
    daily_cost_cap_usd: str = Form(""),
    total_request_cap: str = Form(""),
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> HTMLResponse:
    if len(name) > _MAX_NAME_LEN:
        return _back(next, "!Name too long (max 100)")
    scopes = _validate_scope_list(allowed_scopes or ["llm:chat", "llm:embed"])
    if scopes is None:
        return _back(next, "!Bad or empty scope")
    if not daily_cost_cap_usd.strip():
        # Never unlimited by default: NULL means "no cap" in cost_guard, 0 means
        # "free calls only" (a paid call's estimate always exceeds 0).
        return _back(next, "!Daily cost cap is required (0 = free-only)")
    try:
        cap = _parse_cost_cap(daily_cost_cap_usd)
    except ValueError:
        return _back(next, "!Bad cost cap")
    try:
        req_cap = _parse_request_cap(total_request_cap)
    except ValueError:
        return _back(next, "!Bad request cap (whole number >= 0, blank = unlimited)")
    plain = generate_project_key()
    h = hash_project_key(plain)
    async with get_session() as s:
        row = ProjectRow(
            name=name, owner_email=owner_email or None,
            project_key_hash=h, project_key_prefix=plain[:12],
            allowed_scopes=scopes, daily_cost_cap_usd=cap, total_request_cap=req_cap,
        )
        s.add(row)
        await s.flush()
        new_id = row.id
    await audit(actor="dashboard", action="project.create", target=name,
                metadata={"scopes": scopes}, ip=client_ip(request))
    # POST -> 303 -> GET, so a browser refresh cannot re-submit the form; the
    # key rides in a short-lived encrypted cookie and is shown exactly once.
    resp = RedirectResponse(_flash_url(f"Project {name} created", "/dashboard/projects"),
                            status_code=303)
    dashboard_pages.set_once(resp, plain, name, new_id)
    return resp


@router.post("/dashboard/projects/{project_id}/rotate-token")
async def dash_rotate_project_token(
    project_id: int,
    request: Request,
    next: str = Form(""),
    _: OwnerSession = Depends(require_owner_session),
) -> Response:
    """Issue a fresh project key (2026-10-03, owner request): the stored hash is
    replaced, so the old key stops authenticating at once. The new key is shown
    ONCE on the response (only its hash is kept), exactly like project create."""
    plain = generate_project_key()
    async with get_session() as s:
        row = await s.get(ProjectRow, project_id)
        if not row:
            return _back(next, "!Project not found")
        row.project_key_hash = hash_project_key(plain)
        row.project_key_prefix = plain[:12]
        name = row.name
    await audit(actor="dashboard", action="project.rotate_token", target=name,
                ip=client_ip(request))
    resp = RedirectResponse(_flash_url(f"Token of {name} rotated",
                                       f"/dashboard/projects/{project_id}?tab=settings"),
                            status_code=303)
    dashboard_pages.set_once(resp, plain, name, project_id)
    return resp


_PROBE_CHIP: dict[str, tuple[str, str, str]] = {
    "alive": ("ok", "alive", "жив"),
    "cooldown": ("warn", "rate limited", "лимит запросов"),
    "dead": ("bad", "dead", "мёртв"),
    "neterr": ("warn", "network error", "ошибка сети"),
    "skip": ("off", "no probe for this provider", "для провайдера нет проверки"),
}


@router.post("/dashboard/keys/{key_id}/test", response_class=HTMLResponse)
async def dash_test_key(
    key_id: int,
    request: Request,
    _: OwnerSession = Depends(require_owner_session),
) -> Response:
    """Probe one key right now (the same cheap call the monitor makes) and
    answer with a status chip for the keys page to swap in. Read-only: the key's
    stored state is left to the monitor / real traffic."""
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
    if not row:
        return HTMLResponse(render_html("_key_test.html", cls="bad", en="key not found",
                                        ru="ключ не найден", detail=""), status_code=404)
    try:
        token = decrypt(row.token_encrypted)
    except Exception:
        return HTMLResponse(render_html("_key_test.html", cls="bad", en="token decrypt failed",
                                        ru="не удалось расшифровать токен", detail=""))
    verdict, code, hint = await probe(row.provider, token, row.account_id)
    await audit(actor="dashboard", action="key.test", target=f"{row.provider}/{row.label}",
                metadata={"verdict": verdict, "http": code}, ip=client_ip(request))
    cls, en, ru = _PROBE_CHIP.get(verdict, ("warn", verdict, verdict))
    detail = " · ".join(b for b in (str(code) if code else "", hint) if b)
    return HTMLResponse(render_html("_key_test.html", cls=cls, en=en, ru=ru, detail=detail))
