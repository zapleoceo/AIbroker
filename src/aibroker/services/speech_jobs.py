"""Worker-only, one-shot queued Gemini speech. This module does not admit jobs."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from aibroker.crypto import decrypt
from aibroker.db import get_session
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import ApiKeyRow, DeepJobFileRow, DeepJobRow, ProjectRow, UsageLogRow
from aibroker.providers.catalog import resolve_pin
from aibroker.providers.gemini_tts import estimate_speech_cost, synthesize_via_gemini
from aibroker.providers.registry import model_for
from aibroker.routing import pick_and_reserve, scope_for
from aibroker.routing.budget_admission import _admit_ordinary_locked
from aibroker.routing.budget_ledger import lock_budget, refund_debit
from aibroker.routing.cost_guard import CostGuardError
from aibroker.routing.selector import record_usage
from aibroker.services.media_dispatch import MediaUnknown, media_fingerprint
from aibroker.services.speech_wav import _WAV_MIME, _canonical_wav
from aibroker.telemetry.request_context import current_request_id

_CAP = "speech"
_SCOPE = scope_for(_CAP)
_QUEUED_MODELS = frozenset({
    "gemini/gemini-3.8-flash-tts", "gemini/gemini-3.8-flash-lite-tts",
})
_QUEUED_MODEL_NAMES = frozenset(model.removeprefix("gemini/") for model in _QUEUED_MODELS)
_OUTPUT_LIMIT = 8 * 1024 * 1024
_RESPONSE_LIMIT = 12 * 1024 * 1024
_AUDIO_TTL = timedelta(hours=1)
_UNKNOWN = "speech_outcome_unknown"
_UNAVAILABLE = "speech_worker_unavailable"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _lock_job(s: AsyncSession, job_id: int, claim: datetime) -> DeepJobRow | None:
    hit = await s.execute(update(DeepJobRow).where(
        DeepJobRow.id == job_id, DeepJobRow.capability == _CAP,
        DeepJobRow.status == "running", DeepJobRow.started_at == claim,
    ).values(id=DeepJobRow.id))
    return await s.get(DeepJobRow, job_id) if hit.rowcount == 1 else None


def _finish_row(job: DeepJobRow, status: str, error: str | None = None) -> None:
    job.status, job.error_message, job.completed_at = status, error, _now()
    job.request = {}  # immutable input fingerprint stays in the admission metadata
    job.run_after = None


async def _stop_claim(job_id: int, claim: datetime | None, reason: str = _UNAVAILABLE) -> bool:
    """Terminalize this exact claim only, preventing generic retry on worker exit."""
    if claim is None:
        return False
    async with get_session() as s:
        # Use the same lock order as booking: shared budget lock before job lock.
        await lock_budget(s)
        job = await _lock_job(s, job_id, claim)
        if job is None:
            return False
        receipt = await s.get(MediaDispatchRow, job_id)
        if receipt is not None:
            hold = (await s.get(BudgetReservationRow, receipt.reservation_id)
                    if receipt.reservation_id else None)
            prepared = (receipt.phase == "prepared" and receipt.project_id == job.project_id
                        and receipt.claim == claim
                        and receipt.fingerprint == media_fingerprint(job.project_id, job.request)
                        and hold is not None
                        and (hold.api_key_id, hold.project_id) == (receipt.key_id, job.project_id)
                        and hold.state in ("held", "released"))
            if not prepared:
                # Preserve any possibly spent hold. Never infer safety after the send fence.
                if receipt.phase != "settled":
                    receipt.phase = "unknown"
                _finish_row(job, "error", _UNKNOWN)
                return True
            if hold is not None and hold.state == "held":
                if await s.get(ApiKeyRow, hold.api_key_id) is None:
                    _finish_row(job, "error", "speech_prepared_hold_review")
                    return True
                await refund_debit(s, hold)
                hold.state = "released"
        if job.cancel_requested:
            _finish_row(job, "cancelled")
        else:
            _finish_row(job, "error", reason)
        return True


class _SpeechAlreadyFenced(Exception):
    """A different attempt owns this job's durable send permit."""


class _SpeechDispatch:
    """Prepared lease and provider-send fence for one exact speech job claim."""

    def __init__(self, row: DeepJobRow, claim: datetime, key: ApiKeyRow, model: str):
        self.job_id, self.project_id, self.claim = row.id, row.project_id, claim
        self.key, self.model = key, model
        self.reservation_id: str | None = None
        self.fingerprint = media_fingerprint(row.project_id, row.request)
        self.audio: bytes | None = None

    async def fence(self, s: AsyncSession, *, authorize_http: bool = True) -> DeepJobRow:
        job = await _lock_job(s, self.job_id, self.claim)
        if job is None or job.project_id != self.project_id:
            raise MediaUnknown("stale speech claim")
        if authorize_http:
            project = await s.get(ProjectRow, self.project_id)
            if project is None or not project.is_active or _SCOPE not in project.allowed_scopes:
                raise ValueError("speech project authorization revoked")
            if job.cancel_requested:
                raise ValueError("speech cancelled before dispatch")
            if media_fingerprint(job.project_id, job.request) != self.fingerprint:
                raise ValueError("speech input changed before dispatch")
        return job

    def _key_digest(self) -> str:
        return hashlib.sha256(self.key.token_encrypted.encode()).hexdigest()

    async def _active_key(self, s: AsyncSession) -> ApiKeyRow:
        key = await s.get(ApiKeyRow, self.key.id)
        if (key is None or key.provider != "gemini" or key.tier != "paid"
                or not key.is_active or not key.is_alive or _SCOPE not in key.scopes
                or key.account_id != self.key.account_id
                or key.token_encrypted != self.key.token_encrypted):
            raise ValueError("speech requires an active paid model key")
        return key

    async def prepare(self, project: ProjectRow, estimate: float) -> None:
        async with get_session() as s:
            await lock_budget(s)
            job = await self.fence(s)
            if await s.get(MediaDispatchRow, self.job_id) is not None:
                raise _SpeechAlreadyFenced("speech already owns an attempt")
            await self._active_key(s)
            rid = await _admit_ordinary_locked(s, self.key, project, estimate)
            if rid is None:
                raise ValueError("speech requires a positive paid estimate")
            s.add(MediaDispatchRow(
                job_id=job.id, project_id=job.project_id, fingerprint=self.fingerprint,
                phase="prepared", claim=self.claim, key_id=self.key.id,
                account_id=self.key.account_id, key_fingerprint=self._key_digest(),
                reservation_id=rid, model=self.model,
            ))
        self.reservation_id = rid

    async def begin(self) -> None:
        async with get_session() as s:
            job = await self.fence(s)
            key = await self._active_key(s)
            receipt = await s.get(MediaDispatchRow, self.job_id)
            hold = await s.get(BudgetReservationRow, self.reservation_id) if self.reservation_id else None
            if (receipt is None or receipt.phase != "prepared" or receipt.claim != self.claim
                    or receipt.fingerprint != self.fingerprint or receipt.project_id != self.project_id
                    or receipt.reservation_id != self.reservation_id or receipt.key_id != key.id
                    or receipt.account_id != key.account_id or receipt.key_fingerprint != self._key_digest()
                    or receipt.model != self.model or hold is None or hold.state != "held"
                    or (hold.api_key_id, hold.project_id) != (key.id, job.project_id)):
                raise MediaUnknown("speech send fence does not match prepared lease")
            receipt.phase = "submitting"

    async def finalize(self, s: AsyncSession, usage_id: int) -> None:
        # record_usage calls this in its booking transaction. A commit whose ack was
        # lost can replay finalization, without retrying the provider or adding bytes.
        receipt = await s.get(MediaDispatchRow, self.job_id)
        if (receipt is None or receipt.project_id != self.project_id
                or receipt.claim != self.claim or receipt.fingerprint != self.fingerprint
                or receipt.reservation_id != self.reservation_id
                or receipt.key_id != self.key.id or receipt.model != self.model):
            raise MediaUnknown("speech accounting fence mismatch")
        if receipt.phase == "settled" and receipt.usage_id == usage_id:
            return
        job = await self.fence(s, authorize_http=False)
        usage = await s.get(UsageLogRow, usage_id)
        if (receipt.phase != "submitting" or usage is None or
                (usage.api_key_id, usage.project_id, usage.model) != (
                    receipt.key_id, receipt.project_id, receipt.model)):
            raise MediaUnknown("speech accounting binding mismatch")
        receipt.phase, receipt.usage_id = "settled", usage_id
        if job.cancel_requested:
            _finish_row(job, "cancelled")
        elif self.audio is None:
            # Known completed provider attempt: book the cost even if the output was unusable.
            _finish_row(job, "error", "speech_result_unsupported")
        else:
            expires = _now() + _AUDIO_TTL
            s.add(DeepJobFileRow(
                job_id=job.id, idx=0, data=self.audio, mime=_WAV_MIME,
                sha256=hashlib.sha256(self.audio).hexdigest(), size=len(self.audio),
                expires_at=expires,
            ))
            job.result_meta = {"mime_type": _WAV_MIME, "audio_expires_at": (expires.isoformat() + "Z")}
            _finish_row(job, "done")


def _queued_speech_model(model: str | None) -> str | None:
    use_model = (resolve_pin(model, _CAP, ["gemini"])[0].model if model
                 else model_for("gemini", _CAP))
    return use_model if use_model in _QUEUED_MODELS else None


async def _execute_queued_speech(row: DeepJobRow) -> None:
    claim = row.started_at
    if claim is None:
        return
    async with get_session() as s:
        project = await s.get(ProjectRow, row.project_id)
        old_receipt = await s.get(MediaDispatchRow, row.id)
    if old_receipt is not None:
        return  # only the owning claim can finalize; the stale sweep eventually resolves it
    if (row.cancel_requested or project is None or not project.is_active
            or _SCOPE not in project.allowed_scopes):
        await _stop_claim(row.id, claim)
        return
    req = row.request
    text, voice, style = req["speech_text"], req["speech_voice"], req["speech_style"]
    model = req.get("model")
    provider = "gemini"
    use_model = _queued_speech_model(model)
    if not use_model:
        await _stop_claim(row.id, claim, "speech_model_unavailable")
        return
    key = await pick_and_reserve(provider, scope=_SCOPE, require_tier="paid",
                                 project_id=project.id, models=[use_model])
    if key is None:
        await _stop_claim(row.id, claim)
        return
    dispatch = _SpeechDispatch(row, claim, key, use_model)
    try:
        await dispatch.prepare(project, estimate_speech_cost(use_model, text))
    except _SpeechAlreadyFenced:
        return
    except CostGuardError:
        await _stop_claim(row.id, claim, "speech_budget_exhausted")
        return
    # A failure before begin is resolved only by the prepared-state refund path.
    plain = decrypt(key.token_encrypted)
    await dispatch.begin()  # commit before the single bounded POST
    audio, meta = await synthesize_via_gemini(
        model=use_model, text=text, voice=voice, style=style, api_key=plain,
        max_response_bytes=_RESPONSE_LIMIT, retain_unusable_audio=True,
    )
    served = meta.get("model_served")
    if served is None or (isinstance(served, str) and served in _QUEUED_MODEL_NAMES):
        dispatch.audio = _canonical_wav(audio, meta.get("mime_type"), max_bytes=_OUTPUT_LIMIT)
    valid = dispatch.audio is not None
    await record_usage(
        api_key_id=key.id, project_id=project.id, lease_id=None, provider=provider, model=use_model,
        model_served=meta.get("model_served"), capability=_CAP, workflow=req.get("workflow"),
        tokens_in=meta.get("tokens_in", 0), tokens_out=meta.get("tokens_out", 0),
        cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
        status="ok" if valid else "error", error_kind=None if valid else "SpeechOutputInvalid",
        http_status=200, request_id=current_request_id(), reservation_id=dispatch.reservation_id,
        media_dispatch=dispatch,
    )


async def _purge_speech_audio(limit: int = 200) -> None:
    async with get_session() as s:
        ids = DeepJobFileRow.__table__.alias("speech_audio_candidates")
        stale = (select(ids.c.job_id).join(DeepJobRow, ids.c.job_id == DeepJobRow.id)
                 .where(DeepJobRow.capability == _CAP, ids.c.idx == 0, ids.c.expires_at <= _now())
                 .limit(limit))
        await s.execute(delete(DeepJobFileRow).where(
            DeepJobFileRow.idx == 0, DeepJobFileRow.job_id.in_(stale)))


async def _read_speech_id_by_key(project_id: int, key: str) -> int | None:
    async with get_session() as s:
        return await s.scalar(select(DeepJobRow.id).where(
            DeepJobRow.project_id == project_id, DeepJobRow.capability == _CAP,
            DeepJobRow.idempotency_key == key,
        ))


async def _read_speech_job(project_id: int, job_id: int) -> DeepJobRow | None:
    """Read only receipt metadata after exact scope/project/type authorization."""
    async with get_session() as s:
        return (await s.scalars(select(DeepJobRow).where(
            DeepJobRow.id == job_id, DeepJobRow.project_id == project_id,
            DeepJobRow.capability == _CAP,
        ).options(load_only(
            DeepJobRow.id, DeepJobRow.status, DeepJobRow.result_meta, DeepJobRow.error_message,
        )))).one_or_none()


async def _read_speech_audio(project_id: int, job_id: int) -> tuple[bytes, str] | None:
    async with get_session() as s:
        row = (await s.execute(select(DeepJobFileRow.data, DeepJobFileRow.mime)
                               .join(DeepJobRow, DeepJobRow.id == DeepJobFileRow.job_id)
                               .where(
            DeepJobRow.id == job_id, DeepJobRow.project_id == project_id,
            DeepJobRow.capability == _CAP, DeepJobRow.status == "done",
            DeepJobFileRow.idx == 0, DeepJobFileRow.expires_at > _now(),
            DeepJobFileRow.size > 0, DeepJobFileRow.size <= _OUTPUT_LIMIT,
        ))).one_or_none()
    if row is None:
        return None
    audio = _canonical_wav(bytes(row.data), row.mime, max_bytes=_OUTPUT_LIMIT)
    return (audio, _WAV_MIME) if audio is not None else None
