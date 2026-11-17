"""Durable Gemini video dispatch fence. No network IO or in-memory recovery guarantee."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import ApiKeyRow, DeepJobRow, ProjectRow, UsageLogRow


class MediaUnknown(Exception):
    """A prior submission cannot safely be repeated. Its financial hold survives."""


def media_fingerprint(project_id: int, request: dict) -> str:
    return hashlib.sha256(json.dumps([project_id, request], sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


class MediaDispatch:
    def __init__(self, job_id: int, project_id: int, claim: datetime):
        self.job_id, self.project_id, self.claim = job_id, project_id, claim
        self.key = None
        self.reservation_id = None
        self.model = None

    async def fence(self, session: AsyncSession, *, authorize_http: bool = True) -> DeepJobRow:
        # A write lock also works on SQLite. Compare the exact queue claim.
        result = await session.execute(update(DeepJobRow).where(
            DeepJobRow.id == self.job_id, DeepJobRow.project_id == self.project_id,
            DeepJobRow.status == "running", DeepJobRow.started_at == self.claim
        ).values(id=DeepJobRow.id))
        if result.rowcount != 1:
            raise MediaUnknown("stale or foreign media claim; no submission allowed")
        if authorize_http:
            project = await session.get(ProjectRow, self.project_id)
            if project is None or not project.is_active or "llm:video" not in project.allowed_scopes:
                raise MediaUnknown("media project authorization revoked")
        job = await session.get(DeepJobRow, self.job_id)
        if job.capability != "video" or "vivix" in job.request:
            raise MediaUnknown("dispatch context is restricted to non-Vivix video jobs")
        return job

    async def authorize_http(self) -> None:
        async with get_session() as session:
            await self.fence(session)

    async def load(self) -> dict:
        async with get_session() as session:
            job = await self.fence(session)
            row = await session.get(MediaDispatchRow, self.job_id)
            digest = media_fingerprint(self.project_id, job.request)
            if row is None:
                row = MediaDispatchRow(job_id=self.job_id, project_id=self.project_id,
                                       fingerprint=digest, phase="prepared", claim=self.claim)
                session.add(row)
            elif row.project_id != self.project_id or row.fingerprint != digest:
                raise MediaUnknown("media identity conflict")
            elif row.claim != self.claim:
                if row.phase == "submitting":
                    row.phase = "unknown"
                row.claim = self.claim
            await session.flush()
            return {c.name: getattr(row, c.name) for c in row.__table__.columns}

    def bind(self, key: ApiKeyRow, reservation_id: str | None, model: str) -> None:
        self.key, self.reservation_id, self.model = key, reservation_id, model

    async def validate_input(self, payload: dict, model: str | None, workflow: str | None) -> None:
        async with get_session() as session:
            job = await self.fence(session)
            if (job.request.get("video") != payload or job.request.get("model") != model
                    or job.request.get("workflow") != workflow):
                raise MediaUnknown("media call does not match durable input")

    async def begin(self) -> None:
        async with get_session() as session:
            job = await self.fence(session)
            row = await session.get(MediaDispatchRow, self.job_id)
            if job.cancel_requested:
                raise ValueError("media cancelled before dispatch")
            if row is None or row.phase != "prepared" or row.claim != self.claim:
                raise MediaUnknown("media POST already fenced; resubmission prohibited")
            if row.fingerprint != media_fingerprint(self.project_id, job.request):
                raise MediaUnknown("media input changed before dispatch")
            hold = await session.get(BudgetReservationRow, self.reservation_id) if self.reservation_id else None
            if (self.key is None or hold is None or hold.state != "held"
                    or (hold.api_key_id, hold.project_id) != (self.key.id, self.project_id)):
                raise ValueError("media requires a bound durable reservation before POST")
            key = await session.get(ApiKeyRow, self.key.id)
            if (key is None or key.provider != "gemini" or key.account_id != self.key.account_id
                    or key.token_encrypted != self.key.token_encrypted or not key.is_active
                    or not key.is_alive or "llm:video" not in key.scopes):
                raise ValueError("media account changed before dispatch")
            row.phase, row.key_id = "submitting", self.key.id
            row.account_id, row.reservation_id = self.key.account_id, self.reservation_id
            row.key_fingerprint = hashlib.sha256(self.key.token_encrypted.encode()).hexdigest()
            row.model = self.model

    async def receipt(self, provider_id: str) -> None:
        if not provider_id or len(provider_id) > 255 or any(c in provider_id for c in "/?#\\"):
            raise MediaUnknown("invalid provider receipt; hold retained")
        async with get_session() as session:
            await self.fence(session, authorize_http=False)
            row = await session.get(MediaDispatchRow, self.job_id)
            if row.claim != self.claim or row.phase != "submitting":
                raise MediaUnknown("stale media receipt")
            row.provider_id, row.phase = provider_id, "submitted"

    async def started(self) -> bool:
        async with get_session() as session:
            row = await session.get(MediaDispatchRow, self.job_id)
            return (row is not None and row.project_id == self.project_id and row.phase != "prepared"
                    and row.reservation_id == self.reservation_id)

    async def uncertain(self) -> None:
        async with get_session() as session:
            await self.fence(session, authorize_http=False)
            row = await session.get(MediaDispatchRow, self.job_id)
            if row and row.phase == "submitting" and row.reservation_id == self.reservation_id:
                row.phase = "unknown"

    async def finalize(self, session: AsyncSession, usage_id: int) -> None:
        await self.fence(session, authorize_http=False)
        row = await session.get(MediaDispatchRow, self.job_id)
        if row is None or row.claim != self.claim or row.phase not in ("submitting", "submitted", "settled"):
            raise MediaUnknown("media finalization fence mismatch")
        if row.usage_id is not None and row.usage_id != usage_id:
            raise MediaUnknown("media usage conflict")
        usage = await session.get(UsageLogRow, usage_id)
        if (usage is None or (usage.api_key_id, usage.project_id, usage.model) != (
                row.key_id, self.project_id, row.model) or row.reservation_id != self.reservation_id):
            raise MediaUnknown("media accounting binding mismatch")
        row.usage_id, row.phase = usage_id, "settled"

    async def booked(self, cost: float) -> int | None:
        async with get_session() as session:
            await self.fence(session, authorize_http=False)
            row = await session.get(MediaDispatchRow, self.job_id)
            if row is None or row.usage_id is None:
                return None
            usage = await session.get(UsageLogRow, row.usage_id)
            if (usage is None or usage.project_id != self.project_id
                    or usage.api_key_id != row.key_id or usage.cost_usd != cost):
                raise MediaUnknown("media receipt accounting mismatch; no additional charge")
            return row.usage_id


async def media_resume_key(state: dict, project_id: int) -> ApiKeyRow:
    if state["project_id"] != project_id or not state["provider_id"]:
        raise MediaUnknown("unknown media submission; no POST retry")
    async with get_session() as session:
        key = await session.get(ApiKeyRow, state["key_id"])
        if (key is None or key.provider != "gemini" or key.account_id != state["account_id"]
                or key.tier != "paid" or not key.is_active or not key.is_alive
                or "llm:video" not in key.scopes or hashlib.sha256(
                    key.token_encrypted.encode()).hexdigest() != state["key_fingerprint"]):
            raise MediaUnknown("original media account unavailable; no key rotation")
        return key


async def media_quarantine(job_id: int, claim: datetime | None) -> bool:
    """True blocks retry, including an already quarantined terminal job."""
    async with get_session() as session:
        result = await session.execute(update(DeepJobRow).where(
            DeepJobRow.id == job_id, DeepJobRow.status == "running", DeepJobRow.started_at == claim
        ).values(id=DeepJobRow.id))
        if result.rowcount != 1:
            existing = await session.get(MediaDispatchRow, job_id)
            return bool(existing and existing.phase != "prepared" and not (
                existing.provider_id and existing.phase in ("submitted", "settled")))
        row = await session.get(MediaDispatchRow, job_id)
        if row is None or row.phase == "prepared" or (row.provider_id and row.phase in ("submitted", "settled")):
            return False
        row.phase = "unknown"
        job = await session.get(DeepJobRow, job_id)
        job.status, job.error_message = "error", "media outcome unknown; automatic resubmit prohibited"
        job.completed_at = None
        return True
