"""FastAPI app entry point."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from aibroker import __version__
from aibroker.agent.constants import AgentProjectNotAllowed
from aibroker.agent.policy import AgentPolicyViolation
from aibroker.agent.worker import agent_worker_loop
from aibroker.config import get_settings
from aibroker.db import close_engine, init_engine
from aibroker.logging_setup import configure_logging
from aibroker.routes import admin, agent, dashboard, health, jobs_control, landing, proxy, signup
from aibroker.services.job_queue import dispatcher_loop
from aibroker.services.request_cap import RequestCapExhausted
from aibroker.telemetry.request_context import new_request_id, request_scope, sanitize_request_id


def _configure_logging() -> None:
    configure_logging()                 # stdlib root logging, shared with the agent workers
    level = getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:  # pragma: no cover
    # App start/stop wiring — run only by a real uvicorn (or the Postgres
    # integration tests' `with TestClient`), never by the SQLite unit run that
    # instantiates the module-level TestClient without entering its context.
    _configure_logging()
    await init_engine()
    log = structlog.get_logger()
    # Drain the async-job queue from inside each web worker — the 2 workers
    # coordinate via FOR UPDATE SKIP LOCKED, so this scales with them and needs
    # no separate container. Cancelled cleanly on shutdown; any job left
    # `running` is re-queued by the next worker (see services/job_queue.py).
    stop = asyncio.Event()
    dispatcher = asyncio.create_task(dispatcher_loop(stop))
    agent_worker = (asyncio.create_task(agent_worker_loop(stop))
                    if get_settings().AGENT_WORKER_ENABLED else None)
    log.info("aibroker started", host=get_settings().PUBLIC_HOST)
    try:
        yield
    finally:
        stop.set()
        # Shutdown budget (docker stop_grace_period 30s, SIGKILL after it):
        # uvicorn already spent up to 10 s draining HTTP (--timeout-graceful-
        # shutdown 10) before we got here; dispatcher = in-flight grace (<= 8 s)
        # + release, so 15 s; 10 + 15 = 25 s < 30 s. The dispatcher shields its
        # release, so even this timeout cannot abort the requeue.
        try:
            await asyncio.wait_for(dispatcher, timeout=15)
        except (TimeoutError, asyncio.CancelledError):
            dispatcher.cancel()
        if agent_worker is not None:
            try:
                await asyncio.wait_for(agent_worker, timeout=30)
            except (TimeoutError, asyncio.CancelledError):
                agent_worker.cancel()
        await close_engine()


app = FastAPI(
    title="AIbroker",
    version=__version__,
    description="Centralized key broker for AI provider API keys",
    lifespan=lifespan,
    redoc_url=None,  # Swagger UI at /docs is enough
)

# 2026-10-02 review: no response carried any security header, so the dashboard
# could be framed (clickjacking the key-delete forms) and /login MIME-sniffed.
# Deliberately NO script-src/style-src: the pages use inline <script>/<style>
# and the Telegram login widget loads from telegram.org. HSTS without
# includeSubDomains/preload — only this host is known to be https-only.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=31536000",
}


_SECURITY_HEADERS_RAW = tuple(
    (name.lower().encode("latin-1"), value.encode("latin-1"))
    for name, value in _SECURITY_HEADERS.items()
)


class _SecurityHeadersMiddleware:
    """Pure-ASGI (no BaseHTTPMiddleware task hop, streaming bodies pass untouched):
    add the security headers to every response start. A header a route already set
    is kept."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers.extend(h for h in _SECURITY_HEADERS_RAW if h[0] not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


class _RequestIdMiddleware:
    """Pure-ASGI (no BaseHTTPMiddleware task hop): bind a request id for the whole
    request so every usage_log attempt row carries it, and return it as
    `X-Request-Id`. A route that already set the header (job endpoints answer with
    `job-<id>`) keeps its own value."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = next((v.decode("latin-1") for k, v in scope["headers"]
                         if k == b"x-request-id"), None)
        rid = sanitize_request_id(supplied) or new_request_id()

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", rid.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        with request_scope(rid):
            await self.app(scope, receive, send_with_id)


app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_RequestIdMiddleware)


@app.exception_handler(AgentProjectNotAllowed)
async def _agent_project_not_allowed(_: Request, exc: AgentProjectNotAllowed) -> JSONResponse:
    """403 with a stable body: the project is not in AGENT_ALLOWED_PROJECT_IDS (fail-closed)."""
    return JSONResponse({"error": "agent_project_not_allowed"}, status_code=403)


@app.exception_handler(AgentPolicyViolation)
async def _agent_policy_violation(_: Request, exc: AgentPolicyViolation) -> JSONResponse:
    """403 {"error": agent_policy_missing | model_not_allowed | account_not_allowed}."""
    return JSONResponse({"error": exc.error}, status_code=403)


@app.exception_handler(RequestCapExhausted)
async def _request_cap_exhausted(_: Request, exc: RequestCapExhausted) -> JSONResponse:
    """429 with a stable machine-readable body (not FastAPI's {"detail": ...}) so a
    client can tell "my lifetime allowance is spent" from a transient rate limit."""
    return JSONResponse(
        {"error": "request_cap_exhausted", "limit": exc.limit, "used": exc.used,
         "message": f"This project has used its lifetime allowance of {exc.limit} requests. "
                    "Ask the owner to raise total_request_cap."},
        status_code=429,
    )


app.include_router(landing.router)
app.include_router(health.router)
app.include_router(proxy.router, prefix="/v1")
app.include_router(jobs_control.router, prefix="/v1")
app.include_router(agent.router, prefix="/v1")
app.include_router(signup.router, prefix="/v1")
app.include_router(admin.router, prefix="/admin")
app.include_router(dashboard.router)
