"""Known unsafe paths: strict XFAIL only after proving duplicate mock POSTs.

No network or paid calls. SQLite tests simulate the result of stale reclaim;
the PostgreSQL variant executes real reclaim SQL only with TEST_DATABASE_URL.
These tests expose missing guarantees, not an implementation of a durable guard.
"""
import base64
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from aibroker.config import get_settings
from aibroker.db.engine import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.providers import gemini_video
from aibroker.services import job_queue
from aibroker.services.video_service import video_job_payload
from tests.test_video_service import MP4, REQ, _paid_key, _run, _wire


class DuplicateMediaPost(Exception):
    """Only raised after assertions prove that the mock accepted two POSTs."""


def mock_transport(monkeypatch, failure):
    calls = []

    async def send(method, url, api_key, body, timeout):
        calls.append((method, url))
        posts = sum(m == "POST" for m, _ in calls)
        if failure == "lost_create_response" and posts == 1:
            # The fake provider accepted the operation, but the response was lost.
            raise httpx.ReadTimeout("accepted mock operation; response lost")
        if failure == "known_id_poll_failure" and posts == 1:
            if method == "POST":
                return httpx.Response(200, json={"id": "known-receipt", "status": "in_progress"})
            raise httpx.ReadTimeout("known receipt GET unavailable")
        return httpx.Response(200, json={"id": f"receipt-{posts}", "status": "completed",
                                         "outputs": [{"type": "video", "mime_type": "video/mp4",
                                                      "data": base64.b64encode(MP4).decode()}]})

    monkeypatch.setattr(gemini_video, "_send", send)
    monkeypatch.setattr(get_settings(), "GEMINI_VIDEO_POLL_S", 0)
    return calls


@pytest.mark.xfail(strict=True, raises=DuplicateMediaPost,
                   reason="BLOCKER: ambiguous create/known receipt GET error rotates paid keys")
@pytest.mark.parametrize("failure", ["lost_create_response", "known_id_poll_failure"])
async def test_no_second_post_after_uncertain_or_known_receipt(monkeypatch, failure):
    calls = mock_transport(monkeypatch, failure)
    wire = _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)],
                 gen=gemini_video.generate_via_gemini)
    outcome = await _run()
    posts = [url for method, url in calls if method == "POST"]
    if len(posts) <= 1:
        return  # strict XPASS forces review/removal once the durable guard exists
    assert len(posts) == 2 and len(wire["pick"]) == 2
    assert outcome.key_label == "paid2" and base64.b64decode(outcome.text) == MP4
    if failure == "known_id_poll_failure":
        assert [method for method, _ in calls] == ["POST", "GET", "POST"]
        assert calls[1][1].endswith("/known-receipt")
    else:
        assert [method for method, _ in calls] == ["POST", "POST"]
    raise DuplicateMediaPost(f"{failure}: two accepted mock generations")


async def lost_finish_then_reclaim(monkeypatch, *, postgres_reclaim):
    calls = mock_transport(monkeypatch, "none")
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)],
          gen=gemini_video.generate_via_gemini)
    before = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    async with get_session() as session:
        session.add(ProjectRow(id=1, name="media-risk", project_key_hash="fixture",
                               project_key_prefix="fixture", allowed_scopes=["llm:video"]))
        await session.flush()
        session.add(DeepJobRow(id=1, project_id=1, capability="video", status="running",
                               request={"video": video_job_payload(REQ)}, retry_count=0,
                               started_at=before, cancel_requested=False))
    original_finish = job_queue._finish
    monkeypatch.setattr(job_queue, "_finish", AsyncMock(side_effect=OSError("mock result commit lost")))
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
    await job_queue._execute_guarded(row)
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
        assert row.status == "running" and row.result_text is None
    assert [m for m, _ in calls] == ["POST"]
    if postgres_reclaim:
        await job_queue._requeue_stale_running()
    else:
        # Explicit test input representing stale recovery; NOT testing its PG SQL.
        async with get_session() as session:
            row = await session.get(DeepJobRow, 1)
            row.status, row.started_at, row.retry_count = "pending", None, 1
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
        assert row.status == "pending" and row.retry_count == 1
        row.status, row.started_at = "running", datetime.now(UTC).replace(tzinfo=None)
    monkeypatch.setattr(job_queue, "_finish", original_finish)
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
    await job_queue._execute_guarded(row)
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
        assert row.status == "done" and base64.b64decode(row.result_text) == MP4
    assert [m for m, _ in calls] == ["POST", "POST"]
    raise DuplicateMediaPost("artifact lost before durable finish; reclaimed job generates again")


@pytest.mark.xfail(strict=True, raises=DuplicateMediaPost,
                   reason="BLOCKER: recovered job has no durable submission fence/receipt")
async def test_post_reclaim_execution_does_not_regenerate_lost_result(monkeypatch):
    await lost_finish_then_reclaim(monkeypatch, postgres_reclaim=False)


@pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"),
                    reason="NOT RUN: no authorized isolated PostgreSQL runtime")
@pytest.mark.xfail(strict=True, raises=DuplicateMediaPost,
                   reason="BLOCKER: real stale reclaim can regenerate a paid video")
async def test_postgres_stale_reclaim_does_not_regenerate(monkeypatch):
    await lost_finish_then_reclaim(monkeypatch, postgres_reclaim=True)
