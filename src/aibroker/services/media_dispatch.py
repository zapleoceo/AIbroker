"""Durable paid video dispatch fence. No network IO or in-memory recovery guarantee."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import ApiKeyRow, DeepJobRow, ProjectRow, UsageLogRow
from aibroker.providers.provider_specs.heygen import HEYGEN_SCOPE


class MediaUnknown(Exception):
    """A prior submission cannot safely be repeated. Its financial hold survives."""


RECONCILIATION_REQUIRED = "reconciliation_required"
RECONCILIATION_RUNNING = "reconciliation_manual_running"
UNKNOWN_OUTCOME = "media outcome unknown; automatic resubmit prohibited"
RESOLVED_NO_SUBMISSION = "media outcome resolved: provider confirms no submission"
PROVIDER_BODY_MAX = 1000          # characters of a provider response body kept on the job
PROVIDER_RESPONSES_MAX = 5        # most recent provider responses kept on the job


async def note_provider_response(job_id: int, *, method: str, path: str, status: int | None,
                                 body: str) -> None:
    """Keep an unexpected or failed provider HTTP response on the job (result_meta
    `provider_responses`): method, path, status and the body cut to PROVIDER_BODY_MAX. The
    caller passes only the response body - never request headers or the key."""
    from aibroker.agent.redact import redact

    entry = {"at": datetime.now(UTC).replace(tzinfo=None).isoformat(), "method": method,
             "path": path, "status": status, "body": redact(body or "")[:PROVIDER_BODY_MAX]}
    async with get_session() as session:
        job = await session.get(DeepJobRow, job_id)
        if job is None:
            return
        meta = dict(job.result_meta or {})
        meta["provider_responses"] = [*(meta.get("provider_responses") or []),
                                      entry][-PROVIDER_RESPONSES_MAX:]
        job.result_meta = meta


def _last_response_summary(meta: dict | None) -> str:
    responses = (meta or {}).get("provider_responses") or []
    if not responses:
        return ""
    last = responses[-1]
    body = " ".join(str(last.get("body") or "").split())[:200]
    return f" (last provider response: {last.get('method')} {last.get('path')} " \
           f"HTTP {last.get('status')}: {body})"


async def resolve_unknown_without_submission(job_id: int, project_id: int,
                                             evidence: str) -> dict:
    """Close an `unknown` media job once the provider confirms NO submission exists (no
    receipt was ever stored, and the provider's own list shows no such video): the job
    keeps status error with RESOLVED_NO_SUBMISSION, and its budget hold is released through
    the ordinary idempotent refund. Refuses anything that has a provider receipt."""
    from aibroker.routing.cost_guard import release_cost

    async with get_session() as session:
        job = await session.get(DeepJobRow, job_id)
        receipt = await session.get(MediaDispatchRow, job_id)
        if (job is None or job.project_id != project_id or job.capability != "video"
                or job.status != "error" or receipt is None
                or receipt.project_id != project_id or receipt.phase != "unknown"
                or receipt.provider_id):
            raise MediaUnknown("not an unknown media job without a provider receipt")
        key = await session.get(ApiKeyRow, receipt.key_id) if receipt.key_id else None
        reservation_id = receipt.reservation_id
    if key is not None and reservation_id:
        await release_cost(api_key=key, reservation_id=reservation_id)
    async with get_session() as session:
        job = await session.get(DeepJobRow, job_id)
        job.error_message = f"{RESOLVED_NO_SUBMISSION} ({evidence[:200]})"
        job.completed_at = job.completed_at or datetime.now(UTC).replace(tzinfo=None)
        hold = (await session.get(BudgetReservationRow, reservation_id)
                if reservation_id else None)
    return {"job_id": job_id, "status": "error", "reason": job.error_message,
            "reservation_id": reservation_id, "reservation_state": hold.state if hold else None}


async def media_wait(job_id: int, claim: datetime | None, *, force: bool = False) -> bool:
    """Park a known receipt at a cap, or an interrupted explicit manual pass."""
    async with get_session() as session:
        job = (await session.scalars(select(DeepJobRow).where(
            DeepJobRow.id == job_id, DeepJobRow.status == "running",
            DeepJobRow.started_at == claim).with_for_update())).one_or_none()
        if job is None or (not force and job.error_message != RECONCILIATION_RUNNING):
            return False
        receipt = await session.get(MediaDispatchRow, job_id)
        if (receipt is None or receipt.project_id != job.project_id or not receipt.provider_id
                or receipt.phase not in ("submitted", "settled")):
            return False
        job.status, job.error_message = "error", RECONCILIATION_REQUIRED
        job.completed_at, job.run_after = None, None
        return True


async def claim_media_resume(job_id: int, project_id: int, expected_claim: datetime) -> DeepJobRow:
    """Admin caller grants one bounded execution; never resets automatic retry count."""
    async with get_session() as session:
        # Atomic write fence also serializes concurrent requests on SQLite.
        result = await session.execute(update(DeepJobRow).where(
            DeepJobRow.id == job_id, DeepJobRow.project_id == project_id,
            DeepJobRow.status == "error", DeepJobRow.error_message == RECONCILIATION_REQUIRED,
            DeepJobRow.started_at == expected_claim).values(id=DeepJobRow.id))
        if result.rowcount != 1:
            raise MediaUnknown("manual reconciliation claim conflict")
        job = await session.get(DeepJobRow, job_id)
        receipt = await session.get(MediaDispatchRow, job_id)
        project = await session.get(ProjectRow, project_id)
        if (job.capability != "video" or "vivix" in job.request or receipt is None
                or receipt.project_id != project_id or not receipt.provider_id
                or receipt.phase not in ("submitted", "settled")
                or receipt.fingerprint != media_fingerprint(project_id, job.request)
                or not _project_can_dispatch(project, _video_provider(job))):
            raise MediaUnknown("receipt is not eligible for manual reconciliation")
        state = {c.name: getattr(receipt, c.name) for c in receipt.__table__.columns}
        await media_resume_key(state, project_id)
        hold = await session.get(BudgetReservationRow, receipt.reservation_id)
        if (hold is None or (hold.project_id, hold.api_key_id) != (project_id, receipt.key_id)
                or hold.state not in ("held", "settled") or hold.usage_id != receipt.usage_id):
            raise MediaUnknown("manual reconciliation financial binding conflict")
        job.status, job.started_at = "running", datetime.now(UTC).replace(tzinfo=None)
        job.error_message = RECONCILIATION_RUNNING
        return job


def _video_provider(job: DeepJobRow) -> str:
    provider = (job.request.get("video") or {}).get("provider", "gemini")
    if provider not in {"gemini", "heygen"}:
        raise MediaUnknown("unknown durable video provider")
    return provider


def _project_can_dispatch(project: ProjectRow | None, provider: str) -> bool:
    return bool(project is not None and project.is_active
                and "llm:video" in project.allowed_scopes
                and (provider != "heygen" or HEYGEN_SCOPE in project.allowed_scopes))


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
        job = await session.get(DeepJobRow, self.job_id)
        if authorize_http:
            project = await session.get(ProjectRow, self.project_id)
            if not _project_can_dispatch(project, _video_provider(job)):
                raise MediaUnknown("media project authorization revoked")
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
            if (key is None or key.provider != _video_provider(job) or key.account_id != self.key.account_id
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
        if (key is None or key.provider not in {"gemini", "heygen"}
                or not str(state["model"]).startswith(f"{key.provider}/")
                or key.account_id != state["account_id"]
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
        job.status = "error"
        job.error_message = (UNKNOWN_OUTCOME + _last_response_summary(job.result_meta))[:1000]
        job.completed_at = None
        return True
