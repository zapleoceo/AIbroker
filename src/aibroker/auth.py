"""HTTP auth dependencies.

Two headers:
- X-Admin-Key: bootstrap secret from env, full CRUD over projects/keys
- X-Project-Key: per-project key (hashed in DB), scoped access
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select

from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow

# ─── Project key generation/verification ────────────────────────────────────


def generate_project_key(prefix: str = "aib_prj_") -> str:
    """Return a fresh project key — give to client, store hash only."""
    return prefix + secrets.token_urlsafe(32)


def hash_project_key(key: str) -> str:
    """Constant-time-safe hash (sha256 — sufficient for high-entropy random keys)."""
    return hashlib.sha256(key.encode()).hexdigest()


def verify_project_key(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_project_key(plaintext), stored_hash)


def client_ip(request: Request) -> str:
    """Real client IP for audit rows. Behind Cloudflare+nginx,
    `request.client.host` is the proxy — the original address is the FIRST
    entry of X-Forwarded-For (later entries are the proxies that appended)."""
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


# ─── FastAPI dependencies ───────────────────────────────────────────────────


@dataclass
class AdminCtx:
    actor: str = "admin"


@dataclass
class ProjectCtx:
    project: ProjectRow
    actor: str

    def has_scope(self, scope: str) -> bool:
        return scope in (self.project.allowed_scopes or [])


async def require_admin(
    x_admin_key: str | None = Header(None, alias="X-Admin-Key"),
) -> AdminCtx:
    if not x_admin_key or not hmac.compare_digest(x_admin_key, get_settings().ADMIN_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Admin-Key required",
        )
    return AdminCtx()


async def require_project(
    request: Request,
    x_project_key: str | None = Header(None, alias="X-Project-Key"),
) -> ProjectCtx:
    if not x_project_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Project-Key required",
        )
    h = hash_project_key(x_project_key)
    async with get_session() as s:
        proj = (
            await s.execute(
                select(ProjectRow).where(
                    ProjectRow.project_key_hash == h, ProjectRow.is_active.is_(True)
                )
            )
        ).scalar_one_or_none()
    if proj is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid project key"
        )
    return ProjectCtx(project=proj, actor=f"project:{proj.name}")


def require_scope(scope: str):
    """Factory: require X-Project-Key AND that the project has `scope`."""
    async def dep(ctx: ProjectCtx = Depends(require_project)) -> ProjectCtx:
        if not ctx.has_scope(scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"project lacks scope: {scope}",
            )
        return ctx
    return dep
