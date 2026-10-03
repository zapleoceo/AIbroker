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
from aibroker.routes import dashboard_requests as rq
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


def _req_filter(request: Request) -> tuple[DateRange, rq.RequestFilter]:
    rng = resolve_range(request.query_params, _tz(request))
    return rng, rq.RequestFilter.from_params(request.query_params, rng.start, rng.end)


def _csv_cell(v: Any) -> Any:
    """Neutralise spreadsheet formula injection (=, +, -, @ at the start)."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


_CSV_COLUMNS = ("request_id", "created_at", "type", "project", "workflow", "capability",
                "status", "tries", "provider", "model", "model_served", "tokens_in",
                "tokens_out", "cache_read_tokens", "cache_write_tokens", "cost_usd",
                "wait_ms", "latency_ms", "retries", "error")


def _csv_row(r: dict[str, Any]) -> list[Any]:
    cells = {**r, "type": "job" if r["is_job"] else "direct", "status": r["state"],
             "created_at": r["created_at"].isoformat() + "Z",
             "retries": r["job_retries"], "error": r["job_error"]}
    return [_csv_cell(cells.get(c)) for c in _CSV_COLUMNS]


@router.get("/dashboard/requests.csv")
async def requests_csv(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    _, f = _req_filter(request)
    rows, _ = await rq.query_requests(f, limit=rq.CSV_EXPORT_LIMIT)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_COLUMNS)
    for row in rows:
        w.writerow(_csv_row(row))
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": 'attachment; filename="aibroker-requests.csv"',
        "Cache-Control": "no-store"})


# (en, ru) vocabulary shared by the filter selects and the active-filter chips.
_KIND_LABELS = {"job": ("Queue", "Очередь"), "direct": ("Direct", "Прямой")}
_STATUS_LABELS = {"ok": ("ok", "ок"), "failed": ("failed", "ошибка"),
                  "pending": ("pending", "ожидает"), "running": ("running", "в работе")}
_FILTER_NAMES = {"q": ("ID", "ID"), "type": ("Type", "Тип"), "status": ("Status", "Статус"),
                 "project": ("Project", "Проект"), "workflow": ("Workflow", "Сценарий"),
                 "capability": ("Capability", "Способность"), "model": ("Model", "Модель"), "provider": ("Provider", "Провайдер")}


def _filter_chips(f: rq.RequestFilter, rng: DateRange, projects: list[Any]) -> list[dict[str, Any]]:
    """One removable chip per active filter; its link is the page without it."""
    names = {p.id: p.name for p in projects}
    base = {**f.as_query(), **rng.query}
    chips = []
    for key, query_key, val in (
        ("q", "q", f.search), ("type", "type", f.kind), ("status", "status", f.status),
        ("project", "project", f.project_id), ("workflow", "workflow", f.workflow),
        ("capability", "capability", f.capability), ("provider", "provider", f.provider),
        ("model", "model", f.model),
    ):
        if val is None:
            continue
        en, ru = {"type": _KIND_LABELS, "status": _STATUS_LABELS}.get(key, {}).get(
            val, (names.get(val, str(val)),) * 2)
        chips.append({"name": _FILTER_NAMES[key], "value": (en, ru), "href":
                      "/dashboard/requests?" + urlencode({k: v for k, v in base.items()
                                                          if k != query_key})})
    return chips


def _queue_ctx(jobs: dict[str, Any], request: Request) -> dict[str, Any]:
    """The strip's tiles; each is a filter link, the active one is marked."""
    p = request.query_params
    live = {k: p.get(k, "") for k in ("status", "type")}
    tiles = []
    for key, en, ru, n in (
        ("pending", "pending", "ждут", jobs["by_status"].get("pending", 0)),
        ("running", "running", "в работе", jobs["by_status"].get("running", 0)),
        ("failed", "failed 24h", "сбои 24ч", jobs["failed_24h"]),
    ):
        href = "/dashboard/requests?" + urlencode(
            {"type": "job", "status": key, "range": "7d" if key == "failed" else "all"})
        tiles.append({"key": key, "en": en, "ru": ru, "n": n, "href": href,
                      "active": live["type"] == "job" and live["status"] == key,
                      "bad": key == "failed" and n > 0})
    return {"tiles": tiles, "jobs": jobs,
            "poll_qs": urlencode({k: v for k, v in live.items() if v})}


@router.get("/dashboard/requests/queue")
async def requests_queue_strip(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    return render("_queue_strip.html", **_queue_ctx(await q.job_overview(), request))


@router.get("/dashboard/requests")
async def requests_page(request: Request) -> Response:
    if (r := _guard(request)):
        return r
    rng, f = _req_filter(request)
    now = _utc_now()
    facets_since = max(rng.start or now - timedelta(days=7), now - timedelta(days=7))
    (rows, has_more), facets, projects, jobs = await q.gather(
        rq.query_requests(f), q.request_facets(facets_since), _fetch_projects(),
        q.job_overview())
    base = {**f.as_query(), **rng.query}
    nxt = f"/dashboard/requests?{urlencode({**base, 'page': f.page + 1})}" if has_more else ""
    sort_links = {}
    for key in ("time", "cost", "latency", "tokens"):
        desc = not (f.sort == key and f.desc)
        sort_links[key] = "/dashboard/requests?" + urlencode(
            {**{k: v for k, v in base.items() if k not in ("sort", "dir")},
             "sort": key, "dir": "desc" if desc else "asc"})
    ref = request.query_params.get("open")
    drawer = await _request_drawer_ctx(ref) if ref else None
    for row in rows:
        row["err"] = _friendly_call_error(row["http_status"], row["error_kind"])
    failed_href = "/dashboard/requests?" + urlencode({**base, "status": "failed"})
    return render(
        "requests.html",
        **_ctx(request, "requests", ("Requests", "Запросы"), rng=rng, keep=f.as_query()),
        **_queue_ctx(jobs, request),
        f=f, n_filters=len([k for k in f.as_query() if k not in ("sort", "dir")]),
        rows=rows, next_url=nxt, projects=projects,
        facets=facets, sort_links=sort_links, providers=sorted(provider_names()),
        capabilities=list(CAPABILITY_CHAINS), failed_href=failed_href,
        export_url="/dashboard/requests.csv?" + urlencode(base),
        page=f.page, page_size=rq.REQUEST_PAGE_SIZE, drawer=drawer,
        row_limit=rq.CSV_EXPORT_LIMIT, chips=_filter_chips(f, rng, projects),
        kind_labels=_KIND_LABELS, status_labels=_STATUS_LABELS,
    )


async def _request_drawer_ctx(ref: str) -> dict[str, Any] | None:
    d = await rq.request_detail(ref)
    if d is None:
        return None
    for a in (d["summary"], *d["attempts"]):
        a["err"] = _friendly_call_error(a["http_status"], a["error_kind"])
    return d


@router.get("/dashboard/requests/{ref}")
async def request_drawer(ref: str, request: Request) -> Response:
    if (r := _guard(request)):
        return r
    if not request.headers.get("hx-request"):
        return RedirectResponse(
            f"/dashboard/requests?{urlencode({'open': ref, 'range': 'all'})}", status_code=303)
    return render("_request_drawer.html", drawer=await _request_drawer_ctx(ref), ref=ref)


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
    n_signup = sum(1 for c in cards if c["project"].self_signup)
    only_signup = request.query_params.get("signup") == "1"
    if only_signup:
        cards = [c for c in cards if c["project"].self_signup]
    return render(
        "projects.html", **_ctx(request, "projects", ("Projects", "Проекты"), rng=rng),
        cards=cards, new_key=new_key, new_key_project=new_key_project,
        only_signup=only_signup, n_signup=n_signup,
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
        req=views.request_cap_view(project),
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


# ─── models / audit / settings ───────────────────────────────────────


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
async def jobs_page_moved(request: Request) -> Response:
    """The Jobs page was merged into Requests (queued jobs are rows there)."""
    return RedirectResponse("/dashboard/requests?type=job", status_code=301)


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
