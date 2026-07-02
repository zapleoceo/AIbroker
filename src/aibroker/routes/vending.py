"""Vending mode — broker hands out a plain key with short lease.

Use when the broker doesn't know the wire format for the provider's API.
Client must report usage back via /v1/usage.
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from aibroker.auth import ProjectCtx, require_project
from aibroker.config import get_settings
from aibroker.crypto import decrypt
from aibroker.db import get_session
from aibroker.routing import pick_and_reserve
from aibroker.routing.selector import mark_cooldown, mark_dead, record_usage
from aibroker.telemetry import audit

log = logging.getLogger(__name__)
router = APIRouter(tags=["vending"])


class KeyRequest(BaseModel):
    provider: str
    scope: str
    lease_seconds: int | None = Field(None, ge=10, le=3600)
    workflow: str | None = None
    request_meta: dict[str, Any] = Field(default_factory=dict)


class KeyResponse(BaseModel):
    lease_id: str
    api_key_id: int
    provider: str
    key: str
    expires_at: str


class UsageReport(BaseModel):
    lease_id: str
    status: str = Field(pattern="^(ok|rate_limit|auth_fail|error)$")
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: int | None = None
    model: str | None = None
    error_kind: str | None = None
    http_status: int | None = None
    retry_after_s: int | None = None


@router.post("/key", response_model=KeyResponse)
async def vend_key(body: KeyRequest, request: Request,
                    ctx: ProjectCtx = Depends(require_project)) -> KeyResponse:
    if not ctx.has_scope(body.scope):
        raise HTTPException(403, f"project lacks scope: {body.scope}")
    key = await pick_and_reserve(body.provider, scope=body.scope)
    if key is None:
        raise HTTPException(503, f"no available key for {body.provider}/{body.scope}")

    s = get_settings()
    secs = body.lease_seconds or s.DEFAULT_LEASE_SECONDS
    lease_id = "lse_" + secrets.token_urlsafe(20)
    lease_until = datetime.now(UTC) + timedelta(seconds=secs)

    async with get_session() as ses:
        await ses.execute(
            text(
                "INSERT INTO leases (id, api_key_id, project_id, workflow, "
                "                     request_meta, lease_until) "
                "VALUES (:id, :k, :p, :w, CAST(:m AS JSONB), :u)"
            ),
            {
                "id": lease_id, "k": key.id, "p": ctx.project.id,
                "w": body.workflow,
                "m": _json(body.request_meta),
                "u": lease_until,
            },
        )

    await audit(
        actor=ctx.actor, action="vend",
        target=f"{body.provider}/{body.scope}",
        metadata={"lease_id": lease_id, "api_key_id": key.id, "workflow": body.workflow},
        ip=_client_ip(request),
    )

    return KeyResponse(
        lease_id=lease_id,
        api_key_id=key.id,
        provider=body.provider,
        key=decrypt(key.token_encrypted),
        expires_at=lease_until.isoformat(),
    )


@router.post("/usage")
async def report_usage(body: UsageReport, ctx: ProjectCtx = Depends(require_project)) -> dict:
    async with get_session() as ses:
        lease = (
            await ses.execute(
                text("SELECT api_key_id, project_id, workflow FROM leases WHERE id = :id"),
                {"id": body.lease_id},
            )
        ).first()
    if not lease:
        raise HTTPException(404, "unknown lease_id")
    if lease[1] != ctx.project.id:
        raise HTTPException(403, "lease belongs to another project")

    api_key_id, _, workflow = lease
    capability = None  # unknown at vending mode

    if body.status == "rate_limit":
        until = datetime.now(UTC) + timedelta(seconds=body.retry_after_s or 60)
        await mark_cooldown(api_key_id, until)
    elif body.status == "auth_fail":
        await mark_dead(api_key_id)

    request_id = await record_usage(
        api_key_id=api_key_id, project_id=ctx.project.id, lease_id=body.lease_id,
        provider="?", model=body.model, capability=capability, workflow=workflow,
        tokens_in=body.tokens_in, tokens_out=body.tokens_out,
        cost_usd=body.cost_usd, latency_ms=body.latency_ms,
        status=body.status, error_kind=body.error_kind, http_status=body.http_status,
    )
    return {"recorded": True, "request_id": request_id}


@router.post("/release")
async def release(body: dict, ctx: ProjectCtx = Depends(require_project)) -> dict:
    lease_id = body.get("lease_id")
    if not lease_id:
        raise HTTPException(400, "lease_id required")
    async with get_session() as ses:
        res = await ses.execute(
            text(
                "UPDATE leases SET released_at = now() "
                "WHERE id = :id AND project_id = :p AND released_at IS NULL "
                "RETURNING id"
            ),
            {"id": lease_id, "p": ctx.project.id},
        )
        ok = res.first() is not None
    return {"released": ok}


def _json(d: dict[str, Any]) -> str:
    import json
    return json.dumps(d, ensure_ascii=False, default=str)


def _client_ip(req: Request) -> str | None:
    xff = req.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    return req.client.host if req.client else None
