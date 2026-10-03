"""GET pages (and HTMX fragments) of the admin UI.

Thin by design: resolve the range, fetch with the portable queries, shape with
the pure view-models, render a Jinja template. The state-changing POST
handlers stay in `dashboard.py`.
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from aibroker import __version__
from aibroker.auth_session import require_owner_session
from aibroker.config import get_settings
from aibroker.crypto import decrypt, encrypt
from aibroker.providers.registry import default_models, provider_names
from aibroker.routes import dashboard_queries as q
from aibroker.routes import dashboard_views as views
from aibroker.routes.dashboard_assets import ASSETS_VERSION
from aibroker.routes.dashboard_data import (
    _fetch_keys,
    _fetch_projects,
    _fetch_range_and_proj_spend,
    _fetch_tokens_today,
    _range_where,
)
from aibroker.routes.dashboard_labels import _friendly_call_error, reason_labels
from aibroker.routes.dashboard_range import PRESETS, DateRange, resolve_range
from aibroker.routes.dashboard_scopes import _KNOWN_SCOPES, _scope_checkboxes
from aibroker.routes.dashboard_time import client_tz, today_in
from aibroker.routing.chains import CAPABILITY_CHAINS, usable_scopes_for_provider
from aibroker.services.job_queue import _USAGE_RETENTION_DAYS
from aibroker.web.render import render

router = APIRouter(include_in_schema=False)

# Left rail / tab bar. `primary` items fill the phone tab bar; the rest live
# behind its "More" sheet.
NAV: list[dict[str, Any]] = [
    {"key": "overview", "href": "/dashboard", "icon": "grid", "primary": True,
     "en": "Overview", "ru": "Обзор"},
    {"key": "requests", "href": "/dashboard/requests", "icon": "list", "primary": True,
     "en": "Requests", "ru": "Запросы"},
    {"key": "projects", "href": "/dashboard/projects", "icon": "folder", "primary": True,
     "en": "Projects", "ru": "Проекты"},
    {"key": "keys", "href": "/dashboard/keys", "icon": "key", "primary": True,
     "en": "Keys", "ru": "Ключи"},
    {"key": "models", "href": "/dashboard/models", "icon": "cpu", "primary": False,
     "en": "Models", "ru": "Модели"},
    {"key": "jobs", "href": "/dashboard/jobs", "icon": "layers", "primary": False,
     "en": "Jobs", "ru": "Задачи"},
    {"key": "audit", "href": "/dashboard/audit", "icon": "shield", "primary": False,
     "en": "Audit log", "ru": "Аудит"},
    {"key": "settings", "href": "/dashboard/settings", "icon": "sliders", "primary": False,
     "en": "Settings", "ru": "Настройки"},
]

_RANGE_LABELS = {"today": ("Today", "Сегодня"), "7d": ("7d", "7д"),
                 "30d": ("30d", "30д"), "all": ("All", "Всё")}


def _utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _guard(request: Request) -> Response | None:
    """Owner session (or admin key) or a redirect to /login. An HTMX fragment
    request gets HX-Redirect instead, so the login page is not swapped into a
    drawer."""
    try:
        require_owner_session(request)
    except HTTPException:
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)
    return None


def range_links(path: str, rng: DateRange, keep: dict[str, str] | None = None
                ) -> list[dict[str, Any]]:
    keep = keep or {}
    return [{
        "key": k, "en": _RANGE_LABELS[k][0], "ru": _RANGE_LABELS[k][1],
        "active": rng.key == k, "href": f"{path}?{urlencode({**keep, 'range': k})}",
    } for k in PRESETS]


def _ctx(request: Request, active: str, title: tuple[str, str], *,
         rng: DateRange | None = None, keep: dict[str, str] | None = None,
         **extra: Any) -> dict[str, Any]:
    flash = request.query_params.get("flash", "")
    ctx: dict[str, Any] = {
        "active": active, "nav": NAV, "title": title, "version": __version__,
        "assets_version": ASSETS_VERSION,
        "flash": flash[1:] if flash.startswith("!") else flash,
        "flash_err": flash.startswith("!"),
        "rng": rng, "path": request.url.path,
        "range_links": range_links(request.url.path, rng, keep) if rng else [],
        "keep": keep or {},
    }
    ctx.update(extra)
    return ctx


ONCE_COOKIE = "aib_once"
_ONCE_TTL_S = 300


def set_once(resp: Response, plain: str, project: str, project_id: int | None) -> None:
    """Hand a freshly issued project key to the NEXT page view only: encrypted
    (Fernet, TOKEN_SECRET), short-lived, path-scoped, HttpOnly. Stateless, so it
    works across both uvicorn workers; the viewing page deletes it."""
    payload = json.dumps({"k": plain, "p": project, "i": project_id, "t": int(time.time())})
    resp.set_cookie(ONCE_COOKIE, encrypt(payload), max_age=_ONCE_TTL_S, httponly=True,
                    secure=True, samesite="lax", path="/dashboard")


def take_once(request: Request) -> tuple[str, str, int | None] | None:
    raw = request.cookies.get(ONCE_COOKIE)
    if not raw:
        return None
    try:
        d = json.loads(decrypt(raw))
        if time.time() - int(d["t"]) > _ONCE_TTL_S:
            return None
        return str(d["k"]), str(d["p"]), d.get("i")
    except Exception:
        return None


def _forget_once(resp: Response) -> Response:
    resp.delete_cookie(ONCE_COOKIE, path="/dashboard")
    return resp


def _tz(request: Request) -> Any:
    return client_tz(request.cookies.get("aib_tz"))


# ─── overview ───────────────────────────────────────────────────────────────


@router.get("/dashboard")
async def overview(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    tz = _tz(request)
    rng = resolve_range(request.query_params, tz)
    now = _utc_now()
    cs, ce, bucket = rng.chart_start, rng.chart_end, rng.bucket
    prev = rng.previous()
    today_where = _range_where(today_in(tz), today_in(tz), tz)
    (keys, projects, tokens_today, key_act, cur, series, by_model, p95, act_1h,
     today_spend, proj_stats, jobs) = await q.gather(
        _fetch_keys(), _fetch_projects(), _fetch_tokens_today(tz),
        q.key_activity(now - timedelta(days=7)),
        q.range_totals(rng.start, rng.end), q.time_series(cs, ce, bucket),
        q.usage_by_model_series(cs, ce, bucket), q.latency_percentile(rng.start, rng.end),
        q.provider_activity(now - timedelta(hours=1)),
        _fetch_range_and_proj_spend(*today_where),
        q.project_range_stats(rng.start, rng.end, bucket), q.job_overview(),
    )
    prev_tot = p95_prev = None
    if prev:
        prev_tot, p95_prev = await q.gather(
            q.range_totals(*prev), q.latency_percentile(*prev))
    key_rows = views.build_key_rows(keys, tokens_today, key_act, now)
    groups = views.group_by_provider(key_rows, act_1h)
    cards = views.build_project_cards(projects, proj_stats, today_spend[1])
    return render(
        "overview.html",
        **_ctx(request, "overview", ("Overview", "Обзор"), rng=rng),
        kpis=views.build_kpis(cur, prev_tot, p95, p95_prev, series, groups),
        attention=views.build_attention(key_rows, groups, cards, jobs),
        groups=groups, by_model=by_model, bucket=bucket,
        top_projects=sorted(cards, key=lambda c: (-c["spend"], -c["calls"]))[:5],
        jobs=jobs, empty=cur["calls"] == 0,
    )


# ─── requests ───────────────────────────────────────────────────────────────


def _req_filter(request: Request) -> tuple[DateRange, q.RequestFilter]:
    rng = resolve_range(request.query_params, _tz(request))
    return rng, q.RequestFilter.from_params(request.query_params, rng.start, rng.end)


def _csv_cell(v: Any) -> Any:
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


_CSV_COLUMNS = ("id", "created_at", "project", "workflow", "capability", "provider",
                "model", "model_served", "key_label", "status", "http_status",
                "error_kind", "tokens_in", "tokens_out", "cache_read_tokens",
                "cache_write_tokens", "cost_usd", "latency_ms")


@router.get("/dashboard/requests.csv")
async def requests_csv(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    _, f = _req_filter(request)
    rows, _ = await q.query_requests(f, limit=q.CSV_EXPORT_LIMIT)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_COLUMNS)
    for row in rows:
        w.writerow([_csv_cell(row["created_at"].isoformat() + "Z" if c == "created_at"
                              else row.get(c)) for c in _CSV_COLUMNS])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="aibroker-requests.csv"',
        "Cache-Control": "no-store"})


@router.get("/dashboard/requests")
async def requests_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    rng, f = _req_filter(request)
    now = _utc_now()
    facets_since = max(rng.start or now - timedelta(days=7), now - timedelta(days=7))
    (rows, has_more), facets, projects = await q.gather(
        q.query_requests(f), q.request_facets(facets_since), _fetch_projects())
    base = {**f.as_query(), **rng.query}
    nxt = f"/dashboard/requests?{urlencode({**base, 'page': f.page + 1})}" if has_more else ""
    sort_links = {}
    for key in ("time", "cost", "latency", "tokens"):
        desc = not (f.sort == key and f.desc)
        sort_links[key] = "/dashboard/requests?" + urlencode(
            {**{k: v for k, v in base.items() if k not in ("sort", "dir")},
             "sort": key, "dir": "desc" if desc else "asc"})
    open_id = q._opt_int(request.query_params.get("open"))
    drawer = await _request_drawer_ctx(open_id) if open_id is not None else None
    for row in rows:
        row["err"] = _friendly_call_error(row["http_status"], row["error_kind"])
    return render(
        "requests.html",
        **_ctx(request, "requests", ("Requests", "Запросы"), rng=rng, keep=f.as_query()),
        f=f, n_filters=len([k for k in f.as_query() if k not in ("sort", "dir")]),
        rows=rows, has_more=has_more, next_url=nxt, projects=projects,
        facets=facets, sort_links=sort_links, providers=sorted(provider_names()),
        capabilities=list(CAPABILITY_CHAINS), export_url="/dashboard/requests.csv?" + urlencode(base),
        base_query=base, page=f.page, page_size=q.REQUEST_PAGE_SIZE, drawer=drawer,
        row_limit=q.CSV_EXPORT_LIMIT,
    )


async def _request_drawer_ctx(request_id: int) -> dict[str, Any] | None:
    row = await q.get_request(request_id)
    if row is None:
        return None
    attempts = await q.request_attempts(row)
    for a in (row, *attempts):
        a["err"] = _friendly_call_error(a["http_status"], a["error_kind"])
    return {"row": row, "attempts": attempts}


@router.get("/dashboard/requests/{request_id}")
async def request_drawer(request_id: int, request: Request) -> Response:
    if (r := _guard(request)):
        return r
    if not request.headers.get("hx-request"):
        return RedirectResponse(f"/dashboard/requests?open={request_id}&range=all", status_code=303)
    d = await _request_drawer_ctx(request_id)
    return render("_request_drawer.html", drawer=d, request_id=request_id)


# ─── projects ───────────────────────────────────────────────────────────────


async def render_projects(request: Request, *, new_key: str | None = None,
                          new_key_project: str | None = None) -> Response:
    tz = _tz(request)
    rng = resolve_range(request.query_params, tz)
    today = today_in(tz)
    projects, proj_stats, today_spend = await q.gather(
        _fetch_projects(), q.project_range_stats(rng.start, rng.end, rng.bucket),
        _fetch_range_and_proj_spend(*_range_where(today, today, tz)))
    cards = views.build_project_cards(projects, proj_stats, today_spend[1])
    return render(
        "projects.html", **_ctx(request, "projects", ("Projects", "Проекты"), rng=rng),
        cards=cards, new_key=new_key, new_key_project=new_key_project,
        scope_boxes=_scope_checkboxes(["llm:chat", "llm:embed"], "allowed_scopes"),
    )


@router.get("/dashboard/projects")
async def projects_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    once = take_once(request)
    resp = await render_projects(request, new_key=once[0] if once else None,
                                 new_key_project=once[1] if once else None)
    return _forget_once(resp) if request.cookies.get(ONCE_COOKIE) else resp


@router.get("/dashboard/projects/new")
async def project_new_form(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    return render("_project_form.html", project=None,
                  scope_boxes=_scope_checkboxes(["llm:chat", "llm:embed"], "allowed_scopes"))


async def render_project_detail(request: Request, project_id: int, *,
                                new_key: str | None = None,
                                tab: str | None = None) -> Response:
    tz = _tz(request)
    rng = resolve_range(request.query_params, tz)
    tab = tab or request.query_params.get("tab", "usage")
    if tab not in ("usage", "models", "keys", "settings"):
        tab = "usage"
    projects = await _fetch_projects()
    project = next((p for p in projects if p.id == project_id), None)
    if project is None:
        return RedirectResponse("/dashboard?flash=!Project+not+found", status_code=303)
    d = await q.project_breakdown(project_id, rng.start, rng.end, rng.bucket)
    today = today_in(tz)
    _, today_spend = await _fetch_range_and_proj_spend(*_range_where(today, today, tz))
    t = d["totals"]
    for row in d["recent"]:
        row["err"] = _friendly_call_error(row["http_status"], row["error_kind"])
    cap = project.daily_cost_cap_usd
    spent_today = float(today_spend.get(project_id, 0.0) or 0.0)
    return render(
        "project_detail.html",
        **_ctx(request, "projects", (project.name, project.name), rng=rng, keep={"tab": tab}),
        project=project, d=d, tot=t, tab=tab, new_key=new_key,
        today_spend=spent_today, cap=cap,
        cap_pct=min(100, int(spent_today / cap * 100)) if cap else None,
        scope_boxes=_scope_checkboxes(project.allowed_scopes, "allowed_scopes"),
    )


@router.get("/dashboard/projects/{project_id}")
async def project_detail(project_id: int, request: Request) -> Response:
    if (r := _guard(request)):
        return r
    once = take_once(request)
    key = once[0] if once and once[2] == project_id else None
    resp = await render_project_detail(request, project_id, new_key=key)
    return _forget_once(resp) if request.cookies.get(ONCE_COOKIE) else resp


# ─── keys ───────────────────────────────────────────────────────────────────


def provider_catalogue() -> list[dict[str, Any]]:
    """One entry per routable provider — drives the add-key drawer's provider
    picker, its scope boxes and its 'models the broker will use' hint."""
    out = []
    for p, models in default_models().items():
        usable = sorted(usable_scopes_for_provider(p))
        if not usable:
            continue          # in no chain (mistral since 2026-09-12): a key could never be picked
        caps = list(models)
        scope = ("llm:embed" if "embedding" in caps else
                 "llm:vision" if caps == ["vision"] else
                 "llm:deep" if caps == ["chat:deep"] else "llm:chat")
        out.append({"provider": p, "capabilities": caps,
                    "default_scope": scope if scope in usable else usable[0],
                    "scopes": usable, "models": models})
    order = ["cerebras", "groq", "gemini", "cohere", "openrouter", "deepseek", "openai",
             "anthropic", "voyage", "sambanova", "nvidia", "cloudflare", "zai", "local"]
    out.sort(key=lambda e: order.index(e["provider"]) if e["provider"] in order else 99)
    return out


@router.get("/dashboard/keys")
async def keys_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    tz = _tz(request)
    now = _utc_now()
    keys, tokens_today, act, act_1h = await q.gather(
        _fetch_keys(), _fetch_tokens_today(tz), q.key_activity(now - timedelta(days=1)),
        q.provider_activity(now - timedelta(hours=1)))
    rows = views.build_key_rows(keys, tokens_today, act, now)
    groups = views.group_by_provider(rows, act_1h)
    return render("keys.html", **_ctx(request, "keys", ("Keys & providers", "Ключи и провайдеры")),
                  groups=groups, total=len(rows),
                  alive=sum(g["alive"] for g in groups))


@router.get("/dashboard/keys/new")
async def key_new_form(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    catalogue = provider_catalogue()
    return render("_key_form.html", key=None, catalogue=catalogue,
                  catalogue_map={c["provider"]: c for c in catalogue},
                  scope_boxes=_scope_checkboxes(["llm:chat"]))


@router.get("/dashboard/keys/{key_id}/edit")
async def key_edit_form(key_id: int, request: Request) -> Response:
    if (r := _guard(request)):
        return r
    key = next((k for k in await _fetch_keys() if k.id == key_id), None)
    if key is None:
        return Response("Key not found", status_code=404)
    return render("_key_form.html", key=key, catalogue=[],
                  scope_boxes=_scope_checkboxes(key.scopes or ["llm:chat"], provider=key.provider),
                  reason=reason_labels(key.last_error))


# ─── models / jobs / audit / settings ───────────────────────────────────────


@router.get("/dashboard/models")
async def models_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    observed = await q.observed_model_stats(_utc_now() - timedelta(days=7))
    rows = views.build_model_catalogue(observed)
    return render("models.html", **_ctx(request, "models", ("Models", "Модели")),
                  rows=rows, capabilities=list(CAPABILITY_CHAINS),
                  providers=sorted({r["provider"] for r in rows}))


@router.get("/dashboard/jobs")
async def jobs_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    jobs = await q.job_overview()
    return render("jobs.html", **_ctx(request, "jobs", ("Job queue", "Очередь задач")), jobs=jobs)


@router.get("/dashboard/audit")
async def audit_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    p = request.query_params
    before = q._opt_int(p.get("before"))
    actor, action = q._opt_str(p.get("actor"), 100), q._opt_str(p.get("action"), 50)
    rows, has_more = await q.audit_page(before_id=before, actor=actor, action=action)
    nxt = ""
    if has_more and rows:
        nxt = "/dashboard/audit?" + urlencode(
            {k: v for k, v in (("actor", actor), ("action", action),
                               ("before", rows[-1]["id"])) if v})
    return render("audit.html", **_ctx(request, "audit", ("Audit log", "Журнал аудита")),
                  rows=rows, next_url=nxt, actor=actor or "", action=action or "")


@router.get("/dashboard/settings")
async def settings_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    s = get_settings()
    return render("settings.html", **_ctx(request, "settings", ("Settings", "Настройки")),
                  global_cap=s.GLOBAL_DAILY_CAP_USD, retention=_USAGE_RETENTION_DAYS,
                  host=s.PUBLIC_HOST, tz=str(_tz(request)), scopes=_KNOWN_SCOPES)
