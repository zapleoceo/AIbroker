"""FastAPI app entry point."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI

from aibroker.config import get_settings
from aibroker.db import close_engine, init_engine
from aibroker.routes import admin, dashboard, health, proxy, vending


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
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    await init_engine()
    log = structlog.get_logger()
    log.info("aibroker started", host=get_settings().PUBLIC_HOST)
    yield
    await close_engine()


app = FastAPI(
    title="AIbroker",
    version="0.1.0",
    description="Centralized key broker for AI provider API keys",
    lifespan=lifespan,
    redoc_url=None,  # Swagger UI at /docs is enough
)

app.include_router(health.router)
app.include_router(proxy.router, prefix="/v1")
app.include_router(vending.router, prefix="/v1")
app.include_router(admin.router, prefix="/admin")
app.include_router(dashboard.router)
