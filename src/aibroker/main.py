"""FastAPI app entry point."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from aibroker import __version__
from aibroker.config import get_settings
from aibroker.db import close_engine, init_engine
from aibroker.routes import admin, dashboard, health, landing, proxy, signup
from aibroker.services.job_queue import dispatcher_loop
from aibroker.services.request_cap import RequestCapExhausted
from aibroker.telemetry.request_context import new_request_id, request_scope, sanitize_request_id


def _configure_logging() -> None:
    level = getattr(logging, get_settings().LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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
    log.info("aibroker started", host=get_settings().PUBLIC_HOST)
    try:
        yield
    finally:
        stop.set()
        try:
            await asyncio.wait_for(dispatcher, timeout=10)
        except (TimeoutError, asyncio.CancelledError):
            dispatcher.cancel()
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


@app.middleware("http")
async def _security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)   # never override a route's own
    return response


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


app.add_middleware(_RequestIdMiddleware)


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
app.include_router(signup.router, prefix="/v1")
app.include_router(admin.router, prefix="/admin")
app.include_router(dashboard.router)
