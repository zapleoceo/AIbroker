"""Admin API — CRUD over projects + api_keys. X-Admin-Key required."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from aibroker.auth import AdminCtx, generate_project_key, hash_project_key, require_admin
from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.telemetry import audit

router = APIRouter(tags=["admin"], dependencies=[Depends(require_admin)])


# ─── Projects ───────────────────────────────────────────────────────────────


class ProjectCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
    owner_email: str | None = None
    allowed_scopes: list[str] = Field(default_factory=list)
    daily_cost_cap_usd: float | None = None
    monthly_cost_cap_usd: float | None = None
    notes: str = ""


class ProjectOut(BaseModel):
    id: int
    name: str
    owner_email: str | None
    allowed_scopes: list[str]
    daily_cost_cap_usd: float | None
    monthly_cost_cap_usd: float | None
    is_active: bool
    project_key_prefix: str
    notes: str


class ProjectCreated(ProjectOut):
    project_key: str = Field(description="show ONCE on creation; not retrievable")


@router.post("/projects", response_model=ProjectCreated)
async def create_project(body: ProjectCreate, request: Request,
                          _: AdminCtx = Depends(require_admin)) -> ProjectCreated:
    plain = generate_project_key()
    h = hash_project_key(plain)
    async with get_session() as s:
        row = ProjectRow(
            name=body.name,
            owner_email=body.owner_email,
            project_key_hash=h,
            project_key_prefix=plain[:12],
            allowed_scopes=body.allowed_scopes,
            daily_cost_cap_usd=body.daily_cost_cap_usd,
            monthly_cost_cap_usd=body.monthly_cost_cap_usd,
            notes=body.notes,
        )
        s.add(row)
        await s.flush()
        await audit(
            actor="admin", action="project.create", target=body.name,
            metadata={"scopes": body.allowed_scopes},
            ip=_ip(request),
        )
        return ProjectCreated(
            id=row.id, name=row.name, owner_email=row.owner_email,
            allowed_scopes=row.allowed_scopes,
            daily_cost_cap_usd=row.daily_cost_cap_usd,
            monthly_cost_cap_usd=row.monthly_cost_cap_usd,
            is_active=row.is_active,
            project_key_prefix=row.project_key_prefix,
            notes=row.notes,
            project_key=plain,
        )


@router.get("/projects", response_model=list[ProjectOut])
async def list_projects() -> list[ProjectOut]:
    async with get_session() as s:
        rows = (await s.execute(select(ProjectRow).order_by(ProjectRow.id))).scalars().all()
    return [
        ProjectOut(
            id=r.id, name=r.name, owner_email=r.owner_email,
            allowed_scopes=r.allowed_scopes,
            daily_cost_cap_usd=r.daily_cost_cap_usd,
            monthly_cost_cap_usd=r.monthly_cost_cap_usd,
            is_active=r.is_active,
            project_key_prefix=r.project_key_prefix,
            notes=r.notes,
        )
        for r in rows
    ]


# ─── API keys ───────────────────────────────────────────────────────────────


class ApiKeyCreate(BaseModel):
    provider: str = Field(min_length=2, max_length=50)
    label: str = Field(min_length=1, max_length=100)
    token: str = Field(min_length=8, description="plaintext token; encrypted before storage")
    tier: str = Field(default="free", pattern="^(free|paid|trial)$")
    scopes: list[str] = Field(default_factory=lambda: ["llm:chat"])
    is_reserve: bool = False
    daily_cost_cap_usd: float | None = None
    monthly_cost_cap_usd: float | None = None
    notes: str = ""


class ApiKeyOut(BaseModel):
    id: int
    provider: str
    label: str
    tier: str
    scopes: list[str]
    is_reserve: bool
    is_active: bool
    is_alive: bool
    daily_used: int
    daily_cost_used_usd: float
    daily_cost_cap_usd: float | None
    cooldown_until: str | None
    last_used_at: str | None
    error_count: int
    notes: str


@router.post("/keys", response_model=ApiKeyOut)
async def create_key(body: ApiKeyCreate, request: Request) -> ApiKeyOut:
    async with get_session() as s:
        existing = (
            await s.execute(
                select(ApiKeyRow).where(
                    ApiKeyRow.provider == body.provider, ApiKeyRow.label == body.label
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.token_encrypted = encrypt(body.token)
            existing.tier = body.tier
            existing.scopes = body.scopes
            existing.is_reserve = body.is_reserve
            existing.daily_cost_cap_usd = body.daily_cost_cap_usd
            existing.monthly_cost_cap_usd = body.monthly_cost_cap_usd
            existing.notes = body.notes
            existing.is_active = True
            existing.is_alive = True
            row = existing
            action = "key.update"
        else:
            row = ApiKeyRow(
                provider=body.provider, label=body.label, tier=body.tier,
                scopes=body.scopes, is_reserve=body.is_reserve,
                token_encrypted=encrypt(body.token),
                daily_cost_cap_usd=body.daily_cost_cap_usd,
                monthly_cost_cap_usd=body.monthly_cost_cap_usd,
                notes=body.notes,
            )
            s.add(row)
            await s.flush()
            action = "key.create"
        await audit(actor="admin", action=action,
                    target=f"{body.provider}/{body.label}",
                    metadata={"tier": body.tier, "scopes": body.scopes},
                    ip=_ip(request))
        new_id = row.id
    # Auto-discover real free-tier limits from response headers (best-effort).
    # Outside the txn so a probe failure can't roll back the key.
    from aibroker.providers.auto_discover import discover_and_store
    try:
        await discover_and_store(new_id, body.provider, body.token)
    except Exception:
        pass
    async with get_session() as s:
        refreshed = await s.get(ApiKeyRow, new_id)
        return _key_out(refreshed)


@router.get("/keys", response_model=list[ApiKeyOut])
async def list_keys(provider: str | None = None) -> list[ApiKeyOut]:
    async with get_session() as s:
        q = select(ApiKeyRow).order_by(ApiKeyRow.provider, ApiKeyRow.id)
        if provider:
            q = q.where(ApiKeyRow.provider == provider)
        rows = (await s.execute(q)).scalars().all()
    return [_key_out(r) for r in rows]


@router.post("/keys/{key_id}/disable")
async def disable_key(key_id: int, request: Request) -> dict:
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            raise HTTPException(404, "not found")
        row.is_active = False
        await audit(actor="admin", action="key.disable", target=f"id={key_id}",
                    ip=_ip(request))
    return {"ok": True}


@router.delete("/keys/{key_id}")
async def delete_key(key_id: int, request: Request) -> dict:
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key_id)
        if not row:
            raise HTTPException(404, "not found")
        await audit(actor="admin", action="key.delete",
                    target=f"{row.provider}/{row.label}",
                    ip=_ip(request))
        await s.delete(row)
    return {"ok": True}


def _key_out(row: ApiKeyRow) -> ApiKeyOut:
    return ApiKeyOut(
        id=row.id, provider=row.provider, label=row.label, tier=row.tier,
        scopes=row.scopes, is_reserve=row.is_reserve,
        is_active=row.is_active, is_alive=row.is_alive,
        daily_used=row.daily_used,
        daily_cost_used_usd=row.daily_cost_used_usd,
        daily_cost_cap_usd=row.daily_cost_cap_usd,
        cooldown_until=row.cooldown_until.isoformat() if row.cooldown_until else None,
        last_used_at=row.last_used_at.isoformat() if row.last_used_at else None,
        error_count=row.error_count,
        notes=row.notes,
    )


def _ip(req: Request) -> str | None:
    return req.client.host if req.client else None
