"""Public model-compare page: fan-out of ONE prompt to several catalog models, history, guard.

The page has no login. Every model call goes through Broker's own job path
(`routes.proxy.jobs_submit` / `jobs_poll` logic) AS the dedicated `model-compare` project, in
process: no HTTP client to itself and no project key anywhere near the browser. The page itself
does not check prices: limits are Broker's project caps (dashboard), and whatever Broker's cost
guard decides is stored and shown as the model's result, never bypassed.

One question at a time per browser cookie AND per IP: a request is "active" while any of its
models is not terminal (answer / error / refusal) and it is younger than ACTIVE_TIMEOUT_S, the
safety net that frees a stuck user. State lives in the DB (no per-process memory), so it holds
across restarts and workers. The check and the insert run in ONE transaction under Postgres
advisory locks (per browser id and per IP), so two workers cannot both admit a second question.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, Response
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.agent.constants import AGENT_SCOPE, AgentProjectNotAllowed
from aibroker.agent.policy import AgentPolicyViolation
from aibroker.db import get_session
from aibroker.db.models import CompareRequestRow, CompareResultRow, ProjectRow
from aibroker.providers import catalog
from aibroker.providers.registry import spec_or_default
from aibroker.routing.chains import chain_for
from aibroker.services.llm_service import cap_message

log = logging.getLogger(__name__)

COMPARE_PROJECT = "model-compare"
COMPARE_SCOPES = ["llm:chat", "llm:edit", "llm:deep", AGENT_SCOPE]
MAX_PROMPT_CHARS = 4000
MAX_MODELS = 12
PAGE_SIZE = 20
ACTIVE_TIMEOUT_S = 300          # safety net: a request older than this counts as finished
POLL_INTERVAL_S = 2.0
COOKIE_NAME = "aib_compare"
COOKIE_MAX_AGE = 365 * 24 * 3600
TERMINAL = frozenset({"done", "error", "refused"})
BUSY_MESSAGE = ("Извините, пока идёт ваш предыдущий вопрос, новый отправить нельзя. "
                "Дождитесь ответов всех выбранных моделей — так мы защищаем сервис от перегрузки.")
TIMEOUT_MESSAGE = "таймаут: модель не ответила за 5 минут"

# First match wins; a model is shown only if it serves at least one of these lanes.
CHAT_LANES = ("chat:fast", "chat:smart", "chat:code", "chat:sales", "chat:edit", "chat:deep")
_COOKIE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
# SQLite fallback only (tests, single process). Postgres serialises with advisory locks instead.
_sqlite_lock = asyncio.Lock()
_trackers: set[asyncio.Task[None]] = set()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# ─── model catalog ──────────────────────────────────────────────────────────


def lane_for(capabilities: list[str], provider: str) -> str | None:
    """First chat lane the model is wired for AND whose provider chain includes its provider
    (the same condition Broker's pin resolution enforces); None = not routable as chat."""
    return next((lane for lane in CHAT_LANES
                 if lane in capabilities and provider in chain_for(lane)), None)  # type: ignore[arg-type]


def subscription_lane(capabilities: list[str]) -> str | None:
    """Lane for a Claude/Codex subscription model. Clients (Orchestra) send these as
    `chat:smart` with the pinned `claude-sub/*` / `codex-sub/*` id; Broker then runs the job
    as agent:claude / agent:codex. The provider is added to the lane's chain by jobs_submit
    itself, so no chain check here."""
    if "chat:smart" in capabilities:
        return "chat:smart"
    return next((lane for lane in CHAT_LANES if lane in capabilities), None)


TOP_MODELS = 20


def page_models() -> list[dict[str, Any]]:
    """The models the page offers AND the only ones submit accepts: top TOP_MODELS by
    intelligence index, one per name (owner rule 2026-10-15; replaces the shortlist).
    Same-named models (one model served by several providers / routes) collapse to one, chosen
    deterministically: a subscription route first, then a free-provider route, then the first id.
    Order: score descending, unscored last, then name."""
    best: dict[str, dict[str, Any]] = {}
    for m in chat_models():
        rank = (not m["subscription"], spec_or_default(m["provider"]).paid, m["id"])
        cur = best.get(m["name"])
        if cur is None or rank < cur[0]:
            best[m["name"]] = (rank, m)
    uniq = [m for _rank, m in best.values()]
    uniq.sort(key=lambda m: (m["intelligence_index"] is None,
                             -(m["intelligence_index"] or 0.0), m["name"], m["id"]))
    return uniq[:TOP_MODELS]


def chat_models() -> list[dict[str, Any]]:
    """Chat models Broker's live catalog offers, as {id, name, provider, intelligence_index,
    capability}, best score first (no score last, then by name). The score comes from the
    catalog row itself (exact name+version, else None). Subscription models (claude-sub/*,
    codex-sub/*) are included when the catalog marks them available; they run as agent jobs and
    need the `llm:agent` scope plus the deploy-time agent allowlist/policy for this project.
    A model no chat chain can route to is left out rather than shown as a button that can only
    be refused."""
    out = []
    for row in catalog.listing():
        if row["billing_type"] == "subscription":
            # codex-sub is enabled by the owner's decision of 15.10: the agent runs in the
            # separate container aibroker-agent-worker-codex; the shell of `codex exec` stays
            # (workspace-write, approval never, agent/codex_cli.py) and the owner accepted that.
            if row.get("available") is False:
                continue
            lane = subscription_lane(row["capabilities"])
        else:
            lane = lane_for(row["capabilities"], row["owned_by"])
        if lane is None:
            continue
        out.append({"id": row["id"], "name": row["name"], "provider": row["owned_by"],
                    "intelligence_index": row.get("intelligence_index"), "capability": lane,
                    "subscription": row["billing_type"] == "subscription"})
    out.sort(key=lambda m: (m["intelligence_index"] is None,
                            -(m["intelligence_index"] or 0.0), m["name"], m["id"]))
    return out


# ─── project + guard ────────────────────────────────────────────────────────


async def get_project() -> ProjectRow | None:
    async with get_session() as s:
        return (await s.execute(
            select(ProjectRow).where(ProjectRow.name == COMPARE_PROJECT,
                                     ProjectRow.is_active.is_(True)))).scalar_one_or_none()


def browser_id_from(cookie: str | None) -> str:
    """The caller's anonymous id: the cookie when well-formed, else a fresh one."""
    return cookie if cookie and _COOKIE_RE.match(cookie) else secrets.token_urlsafe(24)


def set_browser_cookie(response: Response, browser_id: str, *, secure: bool) -> None:
    response.set_cookie(COOKIE_NAME, browser_id, max_age=COOKIE_MAX_AGE, httponly=True,
                        samesite="lax", secure=secure, path="/")


async def active_request_id(s: AsyncSession, browser_id: str, ip: str) -> int | None:
    """Newest unfinished request of this browser OR this IP, else None (uses session `s`)."""
    cutoff = _now() - timedelta(seconds=ACTIVE_TIMEOUT_S)
    who = CompareRequestRow.browser_id == browser_id
    if ip:
        who = who | (CompareRequestRow.client_ip == ip)
    pending = (select(CompareResultRow.id)
               .where(CompareResultRow.request_id == CompareRequestRow.id,
                      CompareResultRow.status.not_in(TERMINAL)).exists())
    return (await s.execute(
        select(CompareRequestRow.id)
        .where(who, CompareRequestRow.created_at > cutoff, pending)
        .order_by(CompareRequestRow.id.desc()).limit(1))).scalar_one_or_none()


async def _lock_caller(s: AsyncSession, browser_id: str, ip: str) -> None:
    """Serialise submits of one browser and of one IP across ALL workers: Postgres advisory
    locks held until this transaction ends (always taken browser first, then IP: no deadlock).
    SQLite has no such lock; there the in-process fallback lock is taken by the caller."""
    await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('cmp:b:' || :k))"),
                    {"k": browser_id})
    if ip:
        await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('cmp:i:' || :k))"),
                        {"k": ip})


async def create_request(prompt: str, browser_id: str, ip: str,
                         models: list[dict[str, Any]]) -> int:
    """Guard + insert in ONE transaction, under per-browser and per-IP locks. Raises
    HTTPException(429) while the caller still has an unfinished request."""
    async with get_session() as s:
        sqlite = s.bind.dialect.name == "sqlite"
        if sqlite:
            await _sqlite_lock.acquire()
        try:
            if not sqlite:
                await _lock_caller(s, browser_id, ip)
            if await active_request_id(s, browser_id, ip) is not None:
                raise HTTPException(429, BUSY_MESSAGE)
            now = _now()
            req = CompareRequestRow(prompt=prompt, browser_id=browser_id, client_ip=ip,
                                    created_at=now)
            s.add(req)
            await s.flush()
            for m in models:
                s.add(CompareResultRow(
                    request_id=req.id, model_id=m["id"], capability=m["capability"],
                    intelligence_index=m["intelligence_index"], status="pending", created_at=now))
            request_id = req.id
            await s.commit()        # inside the fallback lock: the next caller sees this row
            return request_id
        finally:
            if sqlite:
                _sqlite_lock.release()


# ─── Broker job path (seams: tests replace submit_model / poll_job) ─────────


class Refused(Exception):
    """Broker refused the call before it ran; str() is the user-facing reason."""


async def submit_model(project: ProjectRow, model_id: str, capability: str, prompt: str) -> int:
    """Enqueue one chat job for `model_id` through Broker's own /v1/jobs logic, as the compare
    project. Returns the job id or raises Refused."""
    from aibroker.auth import ProjectCtx
    from aibroker.routes import proxy
    from aibroker.services.request_cap import RequestCapExhausted

    ctx = ProjectCtx(project=project, actor=f"project:{project.name}")
    try:
        body = proxy.ChatRequest(messages=[{"role": "user", "content": prompt}], model=model_id,
                                 labels={"page": "compare"})
        out = await proxy.jobs_submit(body, Response(), capability, None, None, ctx)
    except HTTPException as exc:
        raise Refused(f"отказ: {exc.detail}") from exc
    except RequestCapExhausted as exc:
        raise Refused(f"отказ: лимит запросов проекта ({exc})") from exc
    except AgentProjectNotAllowed as exc:   # subscription route: project not on the agent allowlist
        raise Refused("отказ: проект не допущен к подписочным моделям "
                      "(agent_project_not_allowed)") from exc
    except AgentPolicyViolation as exc:     # subscription route: model/account outside the policy
        raise Refused(f"отказ: политика проекта ({exc.error})") from exc
    return int(out.job_id)  # type: ignore[union-attr]


async def poll_job(project: ProjectRow, job_id: int) -> Any | None:
    from aibroker.routes import proxy
    from aibroker.services.deep_jobs import get_job

    row = await get_job(job_id, project.id)
    return None if row is None else proxy._job_response(row)


async def cancel_model_job(project: ProjectRow, job_id: int) -> None:
    """Cancel a still-unfinished Broker job with the standard cancel (same as POST
    /v1/jobs/{id}/cancel). Best effort: a failure must not block closing the request."""
    from aibroker.services.job_control import cancel_job

    try:
        await cancel_job(job_id, project.id,
                         agent_allowed=AGENT_SCOPE in (project.allowed_scopes or []))
    except Exception:  # noqa: BLE001
        log.warning("compare: cancel of job %s failed", job_id, exc_info=True)


def is_cap_error(message: str) -> bool:
    """True for Broker's own budget-cap job error. The job row stores only the text (no error
    code), so compare with the exact strings the dispatcher writes (`cap_message`), not a guess."""
    return message in (cap_message("daily"), cap_message("monthly"))


async def fan_out(request_id: int, project: ProjectRow, prompt: str) -> None:
    """One job per result row, concurrently. A refused submit is stored as `refused`."""
    async with get_session() as s:
        rows = (await s.execute(select(CompareResultRow.id, CompareResultRow.model_id,
                                       CompareResultRow.capability)
                                .where(CompareResultRow.request_id == request_id))).all()

    async def one(rid: int, model_id: str, capability: str) -> None:
        try:
            job_id = await submit_model(project, model_id, capability, prompt)
            values: dict[str, Any] = {"job_id": job_id}
        except Refused as exc:
            values = {"status": "refused", "error": str(exc), "completed_at": _now()}
        except Exception as exc:  # noqa: BLE001 - one model's failure must not sink the others
            log.exception("compare submit failed for %s", model_id)
            values = {"status": "error", "completed_at": _now(),
                      "error": f"ошибка отправки: {type(exc).__name__}"}
        async with get_session() as s:
            await s.execute(update(CompareResultRow).where(CompareResultRow.id == rid)
                            .values(**values))

    await asyncio.gather(*(one(r.id, r.model_id, r.capability) for r in rows))


async def sync_request(request_id: int, project: ProjectRow | None = None) -> None:
    """Pull terminal job states into the result rows; time out what is still pending after
    ACTIVE_TIMEOUT_S. Idempotent; called by the poll endpoint and the background tracker."""
    project = project or await get_project()
    async with get_session() as s:
        req = await s.get(CompareRequestRow, request_id)
        pending = (await s.execute(
            select(CompareResultRow).where(CompareResultRow.request_id == request_id,
                                           CompareResultRow.status == "pending"))).scalars().all()
    if req is None or not pending:
        return
    expired = _now() - req.created_at > timedelta(seconds=ACTIVE_TIMEOUT_S)
    for r in pending:
        values: dict[str, Any] | None = None
        job = (await poll_job(project, r.job_id)
               if project is not None and r.job_id is not None else None)
        if job is not None and job.status == "done":
            values = {"status": "done", "answer": job.text, "model_served": job.model_served,
                      "latency_ms": job.latency_ms, "completed_at": _now()}
        elif job is not None and job.status in ("error", "cancelled"):
            err = job.error or job.status
            values = ({"status": "refused", "error": f"отказ: лимит проекта ({err})"}
                      if is_cap_error(err) else {"status": "error", "error": err})
            values["completed_at"] = _now()
        elif expired:
            values = {"status": "error", "error": TIMEOUT_MESSAGE, "completed_at": _now()}
            if project is not None and r.job_id is not None:
                await cancel_model_job(project, r.job_id)
        if values:
            async with get_session() as s:
                await s.execute(update(CompareResultRow).where(
                    CompareResultRow.id == r.id, CompareResultRow.status == "pending")
                    .values(**values))


async def track(request_id: int) -> None:
    """Background poller so an answer is saved even if the page is closed."""
    deadline = _now() + timedelta(seconds=ACTIVE_TIMEOUT_S + 10)
    project = await get_project()
    while _now() < deadline:
        await sync_request(request_id, project)
        async with get_session() as s:
            left = (await s.execute(
                select(func.count()).select_from(CompareResultRow).where(
                    CompareResultRow.request_id == request_id,
                    CompareResultRow.status == "pending"))).scalar_one()
        if not left:
            return
        await asyncio.sleep(POLL_INTERVAL_S)


def spawn_tracker(request_id: int, project: ProjectRow, prompt: str) -> None:
    """Fan the prompt out and keep syncing in the background (fire and forget)."""
    async def run() -> None:
        try:
            await fan_out(request_id, project, prompt)
            await track(request_id)
        except Exception:  # noqa: BLE001
            log.exception("compare tracker failed for request %s", request_id)

    task = asyncio.get_running_loop().create_task(run())
    _trackers.add(task)
    task.add_done_callback(_trackers.discard)


# ─── read side ──────────────────────────────────────────────────────────────


def _result_dict(r: CompareResultRow) -> dict[str, Any]:
    return {"model_id": r.model_id, "intelligence_index": r.intelligence_index,
            "status": r.status, "answer": r.answer, "error": r.error,
            "model_served": r.model_served or "unknown", "latency_ms": r.latency_ms,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None}


def _request_dict(req: CompareRequestRow, results: list[CompareResultRow]) -> dict[str, Any]:
    return {"id": req.id, "prompt": req.prompt, "created_at": req.created_at.isoformat(),
            "done": all(r.status in TERMINAL for r in results),
            "results": [_result_dict(r) for r in sorted(results, key=lambda x: x.id)]}


async def get_request(request_id: int) -> dict[str, Any] | None:
    async with get_session() as s:
        req = await s.get(CompareRequestRow, request_id)
        if req is None:
            return None
        results = (await s.execute(select(CompareResultRow).where(
            CompareResultRow.request_id == request_id))).scalars().all()
    return _request_dict(req, list(results))


def _like_pattern(q: str) -> str:
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


async def search_history(q: str, page: int) -> dict[str, Any]:
    """Newest first, optional case-insensitive substring match on the prompt."""
    page = max(page, 1)
    where = [CompareRequestRow.prompt.ilike(_like_pattern(q), escape="\\")] if q else []
    async with get_session() as s:
        total = (await s.execute(select(func.count()).select_from(CompareRequestRow)
                                 .where(*where))).scalar_one()
        reqs = (await s.execute(
            select(CompareRequestRow).where(*where)
            .order_by(CompareRequestRow.id.desc())
            .limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE))).scalars().all()
        ids = [r.id for r in reqs]
        results = (await s.execute(select(CompareResultRow).where(
            CompareResultRow.request_id.in_(ids)))).scalars().all() if ids else []
    by_req: dict[int, list[CompareResultRow]] = {}
    for r in results:
        by_req.setdefault(r.request_id, []).append(r)
    return {"items": [_request_dict(r, by_req.get(r.id, [])) for r in reqs],
            "page": page, "page_size": PAGE_SIZE, "total": total,
            "has_more": page * PAGE_SIZE < total}
