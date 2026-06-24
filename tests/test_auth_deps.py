"""auth — FastAPI dependencies (require_project / require_admin / scope guards)."""
from __future__ import annotations

import os

import pytest
from fastapi import HTTPException
from sqlalchemy import insert
from starlette.requests import Request

from aibroker.auth import (
    ProjectCtx,
    generate_project_key,
    hash_project_key,
    require_admin,
    require_project,
    require_scope,
)
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow


ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


def _request(headers: dict[str, str] = None) -> Request:
    """Build a minimal ASGI scope so the Request object is usable."""
    scope = {
        "type": "http",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "path": "/",
        "method": "GET",
        "raw_path": b"/",
        "query_string": b"",
    }
    return Request(scope)


async def test_require_admin_passes_with_correct_key():
    ctx = await require_admin(x_admin_key=get_settings().ADMIN_KEY)
    assert ctx.actor == "admin"


async def test_require_admin_missing_key_raises_401():
    with pytest.raises(HTTPException) as exc:
        await require_admin(x_admin_key=None)
    assert exc.value.status_code == 401


async def test_require_admin_wrong_key_raises_401():
    with pytest.raises(HTTPException) as exc:
        await require_admin(x_admin_key="totally-wrong")
    assert exc.value.status_code == 401


async def test_require_project_missing_header_raises():
    req = _request()
    with pytest.raises(HTTPException) as exc:
        await require_project(req, x_project_key=None)
    assert exc.value.status_code == 401


async def test_require_project_wrong_key_raises():
    req = _request({"x-project-key": "aib_prj_fake_does_not_exist"})
    with pytest.raises(HTTPException) as exc:
        await require_project(req, x_project_key="aib_prj_fake_does_not_exist")
    assert exc.value.status_code == 401


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_require_project_valid_key_resolves():
    """Insert a project, then resolve via the dep."""
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            name="resolve_test",
            project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12],
            allowed_scopes=["llm:chat"],
            is_active=True,
            notes="",
        ))
    req = _request({"x-project-key": plain})
    ctx = await require_project(req, x_project_key=plain)
    assert ctx.project.name == "resolve_test"
    assert ctx.actor == "project:resolve_test"


def test_project_ctx_has_scope():
    proj = ProjectRow(
        id=1, name="x", project_key_hash="x", project_key_prefix="x",
        allowed_scopes=["llm:chat", "llm:embed"],
        is_active=True, notes="",
    )
    ctx = ProjectCtx(project=proj, actor="project:x")
    assert ctx.has_scope("llm:chat")
    assert ctx.has_scope("llm:embed")
    assert not ctx.has_scope("admin:write")
    assert not ctx.has_scope("nonexistent")


def test_require_scope_factory_returns_callable():
    """require_scope("X") returns a dep factory."""
    dep = require_scope("llm:chat")
    assert callable(dep)
