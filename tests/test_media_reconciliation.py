"""Bounded explicit admin recovery: real local ledger, synthetic HTTP only."""
import asyncio
import os
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import ApiKeyRow, DeepJobRow, UsageLogRow
from aibroker.routes.admin import router
from aibroker.services import job_queue
from aibroker.services.media_dispatch import (
    RECONCILIATION_REQUIRED,
    MediaUnknown,
    claim_media_resume,
    media_wait,
)
from aibroker.services.video_service import VIDEO_MAX_RETRIES
from tests.test_media_dispatch import media_job as media_job
from tests.test_media_dispatch import run_media
from tests.test_media_resubmit_risk import mock_transport


async def parked(media_job, monkeypatch):
    calls = mock_transport(monkeypatch, "known_id_poll_failure")
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        job.retry_count, job.cancel_requested = VIDEO_MAX_RETRIES, True
    await job_queue._requeue_or_fail(1, VIDEO_MAX_RETRIES, "GET unavailable", media_job[2].claim,
                                     max_retries=VIDEO_MAX_RETRIES)
    return calls


async def admin_resume(claim, *, project=1, admin=True):
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    headers = {"X-Admin-Key": get_settings().ADMIN_KEY} if admin else {"X-Project-Key": "fixture"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/admin/media/jobs/1/reconcile", headers=headers,
                                 json={"project_id": project, "expected_claim": claim.isoformat()})


async def assert_waiting():
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        receipt = await session.get(MediaDispatchRow, 1)
        hold = await session.get(BudgetReservationRow, receipt.reservation_id)
        assert job.status == "error" and job.error_message == RECONCILIATION_REQUIRED
        assert job.completed_at is None and job.retry_count == VIDEO_MAX_RETRIES
        assert hold.state == "held" and hold.project_id == receipt.project_id == job.project_id
        assert hold.api_key_id == receipt.key_id
        return job.started_at


async def test_cancel_at_cap_admin_resume_once_settles(media_job, monkeypatch):
    calls = await parked(media_job, monkeypatch)
    claim = await assert_waiting()
    response = await admin_resume(claim)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    assert (await admin_resume(claim)).status_code == 409
    assert [m for m, _ in calls] == ["POST", "GET", "GET"]
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        hold = await session.get(BudgetReservationRow, receipt.reservation_id)
        assert hold.state == receipt.phase == "settled" and hold.usage_id == receipt.usage_id
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1
        assert (await session.get(DeepJobRow, 1)).retry_count == VIDEO_MAX_RETRIES


async def test_last_automatic_retry_still_allowed_before_cap(media_job, monkeypatch):
    from tests.test_media_dispatch import new_claim

    mock_transport(monkeypatch, "known_id_poll_failure")
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    await job_queue._requeue_or_fail(1, VIDEO_MAX_RETRIES - 1, "GET unavailable", media_job[2].claim,
                                     max_retries=VIDEO_MAX_RETRIES)
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        assert job.status == "pending" and job.retry_count == VIDEO_MAX_RETRIES
    dispatch = await new_claim()
    await job_queue._requeue_or_fail(1, VIDEO_MAX_RETRIES, "GET unavailable", dispatch.claim,
                                     max_retries=VIDEO_MAX_RETRIES)
    await assert_waiting()


async def test_admin_can_read_waiting_claim_without_project_credentials(media_job, monkeypatch):
    calls = await parked(media_job, monkeypatch)
    claim = await assert_waiting()
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/admin/media/jobs/1/reconcile"
        assert (await client.get(url, params={"project_id": 1})).status_code == 401
        headers = {"X-Admin-Key": get_settings().ADMIN_KEY}
        assert (await client.get(url, params={"project_id": 2}, headers=headers)).status_code == 404
        result = await client.get(url, params={"project_id": 1}, headers=headers)
        assert result.status_code == 200
        assert result.json()["reason"] == RECONCILIATION_REQUIRED
        assert result.json()["expected_claim"] == claim.isoformat()
    assert [m for m, _ in calls] == ["POST", "GET"]


@pytest.mark.parametrize("denied", ["nonadmin", "foreign", "stale", "revoked", "unknown"])
async def test_manual_resume_rejects_without_http(media_job, monkeypatch, denied):
    calls = await parked(media_job, monkeypatch)
    claim = await assert_waiting()
    if denied in ("revoked", "unknown"):
        async with get_session() as session:
            if denied == "revoked":
                (await session.get(ApiKeyRow, 1)).is_active = False
            else:
                row = await session.get(MediaDispatchRow, 1)
                row.phase, row.provider_id = "unknown", None
    response = await admin_resume(claim - timedelta(seconds=1) if denied == "stale" else claim,
                                  project=2 if denied == "foreign" else 1, admin=denied != "nonadmin")
    assert response.status_code in (401, 403) if denied == "nonadmin" else response.status_code == 409
    assert [m for m, _ in calls] == ["POST", "GET"]
    await assert_waiting()


async def test_failed_manual_pass_returns_to_waiting_without_polling(media_job, monkeypatch):
    from aibroker.providers import gemini_video

    calls = await parked(media_job, monkeypatch)
    claim = await assert_waiting()

    async def unavailable(method, url, *args):
        calls.append((method, url))
        assert method == "GET"
        raise httpx.ReadTimeout("offline GET failure")

    monkeypatch.setattr(gemini_video, "_send", unavailable)
    result = await admin_resume(claim)
    assert result.status_code == 200 and result.json()["reason"] == RECONCILIATION_REQUIRED
    await assert_waiting()
    assert [m for m, _ in calls] == ["POST", "GET", "GET"]


async def test_manual_restart_and_stale_claim_cannot_requeue(media_job, monkeypatch):
    calls = await parked(media_job, monkeypatch)
    claim = await assert_waiting()
    running = await claim_media_resume(1, 1, claim)
    assert not await media_wait(1, claim, force=True)  # prior worker cannot park new claim
    # New process reconstructs everything from durable rows. Shutdown/death may
    # park it but may never turn the explicit one-pass grant into automatic work.
    assert await job_queue._release_claim(1, running.started_at)
    next_claim = await assert_waiting()
    assert next_claim != claim
    assert (await admin_resume(claim)).status_code == 409
    assert (await admin_resume(next_claim)).json()["status"] == "done"
    assert [m for m, _ in calls] == ["POST", "GET", "GET"]


async def test_concurrent_manual_claim_has_one_winner(media_job, monkeypatch):
    await parked(media_job, monkeypatch)
    claim = await assert_waiting()
    results = await asyncio.gather(claim_media_resume(1, 1, claim), claim_media_resume(1, 1, claim),
                                   return_exceptions=True)
    assert sum(isinstance(r, DeepJobRow) for r in results) == 1
    assert sum(isinstance(r, MediaUnknown) for r in results) == 1


@pytest.mark.parametrize("manual", [False, True])
async def test_stale_media_boundary_and_restart_waiting(media_job, monkeypatch, manual):
    await parked(media_job, monkeypatch)
    claim = await assert_waiting()
    if manual:
        running = await claim_media_resume(1, 1, claim)
        async with get_session() as session:
            (await session.get(DeepJobRow, 1)).started_at = running.started_at - timedelta(hours=1)
    else:
        async with get_session() as session:
            job = await session.get(DeepJobRow, 1)
            job.status, job.error_message = "running", None
            job.retry_count = job_queue._MAX_RETRIES
    await job_queue._reconcile_stale_media()
    # Reconstructed worker ticks cannot make manual waiting eligible again.
    await job_queue._reconcile_stale_media()
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        assert job.status == "error" and job.error_message == RECONCILIATION_REQUIRED
        receipt = await session.get(MediaDispatchRow, 1)
        assert (await session.get(BudgetReservationRow, receipt.reservation_id)).state == "held"


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"),
                    reason="NOT RUN: authorized isolated PostgreSQL unavailable")
async def test_real_stale_death_cap_parks_known_receipt(media_job, monkeypatch):
    mock_transport(monkeypatch, "known_id_poll_failure")
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    async with get_session() as session:
        (await session.get(DeepJobRow, 1)).retry_count = job_queue._MAX_RETRIES
    await job_queue._requeue_stale_running()
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        assert job.status == "error" and job.error_message == RECONCILIATION_REQUIRED
        assert job.retry_count == job_queue._MAX_RETRIES
