"""Durable video regression: real ledger/queue, mock HTTP; PG recovery separate."""
import base64
import os
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import DeepJobRow, UsageLogRow
from aibroker.providers import gemini_video
from aibroker.services import job_queue
from aibroker.services.media_dispatch import MediaUnknown
from tests.test_media_dispatch import media_job as media_job
from tests.test_media_dispatch import new_claim, run_media
from tests.test_video_service import MP4


def mock_transport(monkeypatch, failure):
    calls = []

    async def send(method, url, api_key, body, timeout):
        calls.append((method, url))
        posts = sum(m == "POST" for m, _ in calls)
        if failure == "lost_create_response" and posts == 1:
            # The fake provider accepted the operation, but the response was lost.
            raise httpx.ReadTimeout("accepted mock operation; response lost")
        if failure == "known_id_poll_failure" and posts == 1 and len(calls) <= 2:
            if method == "POST":
                return httpx.Response(200, json={"id": "known-receipt", "status": "in_progress"})
            raise httpx.ReadTimeout("known receipt GET unavailable")
        return httpx.Response(200, json={"id": f"receipt-{posts}", "status": "completed",
                                         "outputs": [{"type": "video", "mime_type": "video/mp4",
                                                      "data": base64.b64encode(MP4).decode()}]})

    monkeypatch.setattr(gemini_video, "_send", send)
    monkeypatch.setattr(get_settings(), "GEMINI_VIDEO_POLL_S", 0)
    return calls


async def test_no_second_post_after_uncertain_create(media_job, monkeypatch):
    calls = mock_transport(monkeypatch, "lost_create_response")
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    with pytest.raises(MediaUnknown):
        await run_media(media_job, await new_claim())
    assert [m for m, _ in calls] == ["POST"]
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        assert receipt.phase == "unknown" and receipt.provider_id is None
        hold = await session.get(BudgetReservationRow, receipt.reservation_id)
        assert hold.state == "held" and hold.amount > 0
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0


async def test_known_receipt_recovers_with_get_only_and_one_booking(media_job, monkeypatch):
    calls = mock_transport(monkeypatch, "known_id_poll_failure")
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        assert receipt.phase == "submitted" and receipt.provider_id == "known-receipt"
    for _ in range(2):
        result = await run_media(media_job, await new_claim())
        assert base64.b64decode(result.text) == MP4
    assert [m for m, _ in calls] == ["POST", "GET", "GET", "GET"]
    assert all(url.endswith("/known-receipt") for method, url in calls if method == "GET")
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        hold = await session.get(BudgetReservationRow, receipt.reservation_id)
        assert receipt.phase == "settled" and hold.state == "settled"
        assert receipt.usage_id == hold.usage_id == result.request_id
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1
        assert await session.scalar(select(func.count()).select_from(BudgetReservationRow)) == 1


async def lost_finish_then_reclaim(media_job, monkeypatch, *, postgres_reclaim):
    calls = mock_transport(monkeypatch, "none")
    original_finish = job_queue._finish
    monkeypatch.setattr(job_queue, "_finish", AsyncMock(side_effect=OSError("mock result commit lost")))
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
    await job_queue._execute_guarded(row)
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
        assert row.status == "running" and row.result_text is None
        assert (await session.get(MediaDispatchRow, 1)).phase == "settled"
    assert [m for m, _ in calls] == ["POST"]
    if postgres_reclaim:
        await job_queue._requeue_stale_running()
        async with get_session() as session:
            assert (await session.get(DeepJobRow, 1)).status == "pending"
    # SQLite supplies a new claim explicitly; actual PG stale-reclaim SQL is a separate test.
    await new_claim()
    monkeypatch.setattr(job_queue, "_finish", original_finish)
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
    await job_queue._execute_guarded(row)
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
        assert row.status == "done" and base64.b64decode(row.result_text) == MP4
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1
    assert [m for m, _ in calls] == ["POST", "GET"]


async def test_post_reclaim_execution_does_not_regenerate_lost_result(media_job, monkeypatch):
    await lost_finish_then_reclaim(media_job, monkeypatch, postgres_reclaim=False)


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"),
                    reason="NOT RUN: no authorized isolated PostgreSQL runtime")
async def test_postgres_stale_reclaim_does_not_regenerate(media_job, monkeypatch):
    await lost_finish_then_reclaim(media_job, monkeypatch, postgres_reclaim=True)
