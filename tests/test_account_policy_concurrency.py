"""PostgreSQL policy conversion and stale-form serialization."""
from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator

import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.agent import project_access
from aibroker.agent.account_choices import ACCOUNT_ONLY, NO_ACCOUNTS
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.routes import dashboard
from tests import dash_seed

pytestmark = pytest.mark.skipif(
    "postgres" not in os.environ.get("TEST_DATABASE_URL", ""),
    reason="requires isolated PostgreSQL *_test database",
)


@pytest.mark.parametrize("chosen", [[], ["codex-sub/owner"]])
async def test_concurrent_legacy_save_cannot_erase_account_policy(
    monkeypatch: pytest.MonkeyPatch, chosen: list[str],
) -> None:
    monkeypatch.setattr(get_settings(), "AGENT_ACCOUNT_ONLY_UI_ENABLED", True)
    monkeypatch.setattr(project_access, "_snapshot", {})
    async with get_session() as session:
        session.add(dash_seed.project(9719, "policy-race", agent_enabled=True,
                                     agent_models=["*"], agent_accounts=["owner"]))

    conversion_staged = asyncio.Event()
    release_conversion = asyncio.Event()

    @contextlib.asynccontextmanager
    async def controlled_session() -> AsyncIterator[AsyncSession]:
        async with get_session() as session:
            task = asyncio.current_task()
            role = task.get_name() if task else ""
            if role == "legacy-save":
                # PostgreSQL, not a scheduling delay, proves the concurrent row is locked.
                await session.execute(text("SET LOCAL lock_timeout = '100ms'"))
            yield session
            if role == "account-conversion":
                conversion_staged.set()
                await release_conversion.wait()

    monkeypatch.setattr(dashboard, "get_session", controlled_session)
    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1)})

    async def save(mode: str) -> RedirectResponse:
        return await dashboard.dash_project_agent(
            project_id=9719, request=request, agent_enabled="1",
            agent_models=[] if not mode else None,
            agent_accounts=chosen if mode else [], agent_access_mode=mode,
            confirm_account_access="1" if mode else "", next="", _=None,
        )

    conversion = asyncio.create_task(save("accounts-v1"), name="account-conversion")
    legacy = None
    try:
        async with asyncio.timeout(10):
            await conversion_staged.wait()
            legacy = asyncio.create_task(save(""), name="legacy-save")
            with pytest.raises(DBAPIError) as blocked:
                await legacy
            assert blocked.value.orig.sqlstate == "55P03"  # lock_not_available
            blocked_sql = " ".join((blocked.value.statement or "").upper().split())
            assert blocked_sql.startswith("SELECT ")
            assert " FOR UPDATE" in blocked_sql
            release_conversion.set()
            converted = await conversion
            assert "updated" in converted.headers["location"]
            # A retried stale form must inspect the committed marker and refuse its write.
            stale = await save("")
            assert "Reload" in stale.headers["location"]
    finally:
        release_conversion.set()
        for task in (conversion, legacy):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (conversion, legacy) if t is not None),
                             return_exceptions=True)

    async with get_session() as session:
        row = await session.get(ProjectRow, 9719)
        assert row is not None
        assert row.agent_enabled is True
        assert row.agent_models == [ACCOUNT_ONLY]
        assert row.agent_accounts == (chosen or [NO_ACCOUNTS])
