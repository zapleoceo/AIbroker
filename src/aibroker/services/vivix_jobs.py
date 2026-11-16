"""Vivix bridge into ordinary durable jobs; all production entry points remain disabled."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from aibroker.crypto import decrypt
from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, DeepJobRow, ProjectRow
from aibroker.db.vivix_models import VivixJobRow
from aibroker.providers.gemini_video import VideoRejected
from aibroker.providers.vivix_admission import admit
from aibroker.providers.vivix_inputs import VivixJobRequest
from aibroker.providers.vivix_media import validate_media
from aibroker.services import vivix_runtime
from aibroker.services.deep_jobs import (
    _finish,
    build_request,
    payload_hash,
    replayed_job_id,
    submit_job_ex,
)
from aibroker.services.request_cap import admit_request
from aibroker.services.vivix_billing import BrokerVivixStore
from aibroker.services.vivix_service import VivixService
from aibroker.services.vivix_store import Snapshot

VIVIX_FIELD = "vivix"


class VivixDisabled(VideoRejected):
    pass


async def submit(project: ProjectRow, request: VivixJobRequest, idempotency_key: str) -> int:
    runtime = vivix_runtime.get_runtime()
    if runtime is None:
        raise VivixDisabled("Vivix disabled: verified account, pricing and activation are required")
    payload = build_request(messages=[], model=request.model, max_tokens=0, temperature=0,
                            response_format=None, workflow=request.workflow,
                            extra={VIVIX_FIELD: request.inputs})
    replay = await replayed_job_id(project.id, idempotency_key, payload_hash(project.id, "video", payload))
    if replay is not None:
        return replay
    auth = await runtime.authorize(project, request.model, request.inputs)
    if not auth.materials_verified:
        raise VideoRejected("Vivix media evidence is required")
    validate_media(request.model, request.inputs, auth.media)
    admit(model=request.model, inputs=request.inputs, account_id=auth.account_id,
          allowed_models=auth.allowed_models, quote=auth.quote, now=datetime.now(UTC).timestamp())
    await admit_request(project)
    outcome = await submit_job_ex(project=project, capability="video", messages=[],
                                  model=request.model, max_tokens=0, temperature=0,
                                  response_format=None, workflow=request.workflow,
                                  extra={VIVIX_FIELD: request.inputs}, idempotency_key=idempotency_key)
    return outcome.job_id


async def _key(snapshot: Snapshot) -> str:
    async with get_session() as session:
        key = await session.get(ApiKeyRow, snapshot.body["broker_key_id"])
        if (key is None or key.provider != "vivix" or key.account_id != snapshot.account_id
                or key.tier != "paid" or not key.is_active or not key.is_alive
                or "llm:video" not in key.scopes):
            raise VideoRejected("Vivix bound key unavailable; reservation retained")
        return decrypt(key.token_encrypted)


async def _pending(row: DeepJobRow, state: str) -> None:
    async with get_session() as session:
        await session.execute(update(DeepJobRow).where(
            DeepJobRow.id == row.id, DeepJobRow.started_at == row.started_at,
            DeepJobRow.status == "running").values(
                status="pending", started_at=None,
                run_after=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=10),
                result_meta={"provider": "vivix", "vivix_state": state, "cost_usd": None},
                error_message=None))


async def execute(row: DeepJobRow, project: ProjectRow) -> None:
    runtime = vivix_runtime.get_runtime()
    if runtime is None:
        await _finish(row.id, status="error", error_message="Vivix disabled; no provider call",
                      expect_started_at=row.started_at)
        return
    store = BrokerVivixStore()
    service = VivixService(store, runtime.transport)
    try:
        if not project.is_active or "llm:video" not in project.allowed_scopes:
            raise VideoRejected("Vivix project no longer eligible")
        async with get_session() as session:
            exists = await session.get(VivixJobRow, str(row.id))
        if exists is None:
            if row.cancel_requested:
                await _finish(row.id, status="cancelled", expect_started_at=row.started_at)
                return
            request = VivixJobRequest(provider="vivix", model=row.request["model"],
                                      inputs=row.request[VIVIX_FIELD])
            auth = await runtime.authorize(project, request.model, request.inputs)
            if not auth.materials_verified:
                raise VideoRejected("Vivix media evidence is required")
            validate_media(request.model, request.inputs, auth.media)
            snapshot = await service.prepare(
                job_id=str(row.id), project_id=str(project.id), account_id=auth.account_id,
                model=request.model, inputs=request.inputs, allowed_models=auth.allowed_models,
                quote=auth.quote, limits=auth.limits, broker_key_id=auth.api_key_id,
                workflow=row.request.get("workflow"))
        else:
            snapshot = await store.load(str(row.id), str(project.id))
        if row.cancel_requested:
            snapshot = await service.cancel(job_id=snapshot.id, project_id=snapshot.project_id)
        if snapshot.body["state"] in {"prepared", "submitted"}:
            snapshot = await service.advance(job_id=snapshot.id, project_id=snapshot.project_id,
                                             account_id=snapshot.account_id, api_key=await _key(snapshot))
        if snapshot.body["state"] in {"terminal", "unknown"}:
            receipt = await runtime.receipt(snapshot)
            if receipt is not None:
                snapshot = await service.reconcile(job_id=snapshot.id, project_id=snapshot.project_id,
                                                    actual_usd=receipt.actual_usd,
                                                    evidence=receipt.evidence)
    except VideoRejected:
        await _finish(row.id, status="error", error_message="Vivix admission/binding refused",
                      expect_started_at=row.started_at)
        return
    except Exception:
        # DB/lookup failure resumes through the ledger. Never use the generic retry/key walk.
        await _pending(row, "recovery_pending")
        return
    state = snapshot.body["state"]
    if state in {"submitting", "unknown"}:
        await _finish(row.id, status="error", error_message="Vivix submission requires reconciliation",
                      expect_started_at=row.started_at)
    elif state in {"cancelled", "expired"}:
        await _finish(row.id, status="cancelled" if state == "cancelled" else "error",
                      error_message=None if state == "cancelled" else "Vivix quote expired",
                      expect_started_at=row.started_at)
    elif state == "settled":
        success = snapshot.body.get("provider_status") == "succeeded"
        await _finish(row.id, status="done" if success else "error", result_meta={
            "provider": "vivix", "model": snapshot.body["model"],
            "video_url": snapshot.body.get("url"), "mime_type": "video/mp4",
            "cost_usd": float(snapshot.settled_usd), "request_id": snapshot.body.get("usage_id"),
            "billing_status": "settled"}, error_message=None if success else "Vivix generation failed",
            expect_started_at=row.started_at)
    else:
        await _pending(row, state)
