"""Real SQLite dispatch/accounting state; mocked transport only. PG uses same tests."""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import ApiKeyRow, DeepJobRow, ProjectRow, UsageLogRow
from aibroker.services import video_service
from aibroker.services.media_dispatch import MediaDispatch, MediaUnknown
from tests.test_video_service import REQ


@pytest.fixture
async def media_job(monkeypatch):
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 100)
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda _: "FAKE")
    start = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=30)
    async with get_session() as session:
        session.add(ProjectRow(id=1, name="media-fixture", project_key_hash="h", project_key_prefix="p",
                               allowed_scopes=["llm:video"], daily_cost_cap_usd=10, monthly_cost_cap_usd=10))
        await session.flush()
        session.add(ApiKeyRow(id=1, provider="gemini", label="media-fixture", tier="paid",
                              token_encrypted="FAKE", account_id="fixture-account", scopes=["llm:video"],
                              daily_cost_cap_usd=10, monthly_cost_cap_usd=10, is_active=True, is_alive=True))
        session.add(DeepJobRow(id=1, project_id=1, capability="video", status="running", started_at=start,
                               request={"video": video_service.video_job_payload(REQ)}, retry_count=0,
                               cancel_requested=False))
    async with get_session() as session:
        project, key = await session.get(ProjectRow, 1), await session.get(ApiKeyRow, 1)
    monkeypatch.setattr(video_service, "pick_and_reserve", AsyncMock(return_value=key))
    return project, key, MediaDispatch(1, 1, start)


async def run_media(media_job, dispatch=None):
    return await video_service.run_video(project=media_job[0], request=REQ, model=None,
                                         workflow=None, dispatch=dispatch or media_job[2])


async def new_claim():
    async with get_session() as session:
        row = await session.get(DeepJobRow, 1)
        row.status, row.started_at = "running", datetime.now(UTC).replace(tzinfo=None)
        stamp = row.started_at
    return MediaDispatch(1, 1, stamp)


async def test_wrong_project_or_stale_claim_cannot_prepare(media_job):
    with pytest.raises(MediaUnknown):
        await MediaDispatch(1, 2, media_job[2].claim).load()
    await new_claim()
    with pytest.raises(MediaUnknown):
        await media_job[2].load()
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(MediaDispatchRow)) == 0


async def test_actual_call_must_match_durable_payload(media_job, monkeypatch):
    from dataclasses import replace

    from aibroker.providers import gemini_video
    send = AsyncMock(side_effect=AssertionError("must not perform HTTP"))
    monkeypatch.setattr(gemini_video, "_send", send)
    with pytest.raises(MediaUnknown, match="durable input"):
        await video_service.run_video(project=media_job[0], request=replace(REQ, prompt="different"),
                                       model=None, workflow=None, dispatch=media_job[2])
    send.assert_not_awaited()
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(BudgetReservationRow)) == 0


async def test_parallel_begin_has_one_winner(media_job):
    from aibroker.routing.cost_guard import reserve_cost
    dispatch = media_job[2]
    await dispatch.load()
    rid = await reserve_cost(api_key=media_job[1], project=media_job[0], estimated_cost=1)
    dispatch.bind(media_job[1], rid, "fixture-model")
    results = await asyncio.gather(dispatch.begin(), dispatch.begin(), return_exceptions=True)
    assert sum(r is None for r in results) == 1
    assert sum(type(r) is MediaUnknown for r in results) == 1
    async with get_session() as session:
        assert (await session.get(MediaDispatchRow, 1)).phase == "submitting"
        assert (await session.get(BudgetReservationRow, rid)).state == "held"


async def test_restart_after_fence_before_post_is_unknown(media_job):
    from aibroker.routing.cost_guard import reserve_cost
    dispatch = media_job[2]
    await dispatch.load()
    rid = await reserve_cost(api_key=media_job[1], project=media_job[0], estimated_cost=1)
    dispatch.bind(media_job[1], rid, "fixture-model")
    await dispatch.begin()
    replacement = await new_claim()
    assert (await replacement.load())["phase"] == "unknown"
    with pytest.raises(MediaUnknown):
        await replacement.begin()
    with pytest.raises(MediaUnknown):
        await dispatch.receipt("late-receipt")
    async with get_session() as session:
        assert (await session.get(BudgetReservationRow, rid)).state == "held"
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0


async def test_parallel_attempt_loser_refunds_only_its_own_hold(media_job, monkeypatch):
    from tests.test_media_resubmit_risk import mock_transport
    calls = mock_transport(monkeypatch, "none")
    original = MediaDispatch.begin
    ready = asyncio.Event()
    entered = []

    async def simultaneous_begin(self):
        entered.append(self.reservation_id)
        if len(entered) == 2:
            ready.set()
        await asyncio.wait_for(ready.wait(), 10)
        await original(self)

    monkeypatch.setattr(MediaDispatch, "begin", simultaneous_begin)
    monkeypatch.setattr("aibroker.services.attempt._penalize", AsyncMock(return_value="error"))
    other = MediaDispatch(1, 1, media_job[2].claim)
    results = await asyncio.gather(run_media(media_job), run_media(media_job, other), return_exceptions=True)
    assert sum(isinstance(r, video_service.VideoOutcome) for r in results) == 1, results
    assert sum(type(r) is MediaUnknown for r in results) == 1, results
    assert [m for m, _ in calls] == ["POST"]
    async with get_session() as session:
        holds = (await session.scalars(select(BudgetReservationRow))).all()
        assert len(holds) == 2 and all(h.state == "settled" for h in holds)
        usage = (await session.scalars(select(UsageLogRow))).all()
        assert len(usage) == 2 and sum(u.cost_usd > 0 for u in usage) == 1
        key = await session.get(ApiKeyRow, 1)
        assert key.daily_cost_used_usd == pytest.approx(sum(u.cost_usd for u in usage))


async def test_cancel_after_fence_is_quarantined_on_shutdown(media_job, monkeypatch):
    from aibroker.providers import gemini_video
    from aibroker.services.job_queue import _release_claim, _requeue_or_fail
    calls = []

    async def cancelled(*args):
        calls.append(args[0])
        raise asyncio.CancelledError()

    monkeypatch.setattr(gemini_video, "_send", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await run_media(media_job)
    assert await _release_claim(1, media_job[2].claim)
    await _requeue_or_fail(1, 0, "cancelled", expect_started_at=media_job[2].claim)
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        assert job.status == "error" and "unknown" in job.error_message
        receipt = await session.get(MediaDispatchRow, 1)
        assert receipt.phase == "unknown"
        assert (await session.get(BudgetReservationRow, receipt.reservation_id)).state == "held"
    assert calls == ["POST"]


@pytest.mark.parametrize("fault", ["no_key", "decrypt", "validation"])
async def test_proven_pre_dispatch_failures_do_not_mark_unknown(media_job, monkeypatch, fault):
    from aibroker.providers import gemini_video
    from aibroker.providers.gemini_video import VideoRejected
    send = AsyncMock(side_effect=AssertionError("must not perform HTTP"))
    monkeypatch.setattr(gemini_video, "_send", send)
    monkeypatch.setattr("aibroker.services.attempt._penalize", AsyncMock(return_value="error"))
    monkeypatch.setattr(video_service, "pick_and_reserve", AsyncMock(
        side_effect=[None] if fault == "no_key" else [media_job[1], None]))

    def fail(*args):
        raise VideoRejected("fixture validation")

    if fault == "decrypt":
        monkeypatch.setattr("aibroker.services.attempt.decrypt", fail)
    elif fault == "validation":
        monkeypatch.setattr(gemini_video, "interaction_body", fail)
    if fault == "no_key":
        assert await run_media(media_job) is None
    else:
        with pytest.raises(VideoRejected):
            await run_media(media_job)
    send.assert_not_awaited()
    async with get_session() as session:
        assert (await session.get(MediaDispatchRow, 1)).phase == "prepared"
        holds = (await session.scalars(select(BudgetReservationRow))).all()
        assert all(h.state == "settled" for h in holds)
        assert (await session.get(ApiKeyRow, 1)).daily_cost_used_usd == 0


async def test_atomic_booking_failure_retains_hold_then_get_recovers(media_job, monkeypatch):
    from tests.test_media_resubmit_risk import mock_transport
    calls = mock_transport(monkeypatch, "none")
    original = MediaDispatch.finalize

    async def fail(*args):
        raise ValueError("fixture finalization failure")

    monkeypatch.setattr(MediaDispatch, "finalize", fail)
    with pytest.raises(ValueError, match="finalization failure"):
        await run_media(media_job)
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        assert receipt.phase == "submitted"
        assert (await session.get(BudgetReservationRow, receipt.reservation_id)).state == "held"
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0
    monkeypatch.setattr(MediaDispatch, "finalize", original)
    await run_media(media_job, await new_claim())
    assert [m for m, _ in calls] == ["POST", "GET"]
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1


@pytest.mark.parametrize("changed", ["credential", "project_scope", "request"])
async def test_recovery_rejects_changed_binding_without_http(media_job, monkeypatch, changed):
    from tests.test_media_resubmit_risk import mock_transport
    calls = mock_transport(monkeypatch, "known_id_poll_failure")
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    async with get_session() as session:
        if changed == "credential":
            (await session.get(ApiKeyRow, 1)).token_encrypted = "rotated-fixture"
        elif changed == "project_scope":
            (await session.get(ProjectRow, 1)).allowed_scopes = []
        else:
            row = await session.get(DeepJobRow, 1)
            row.request = {**row.request, "workflow": "changed"}
    with pytest.raises(MediaUnknown):
        await run_media(media_job, await new_claim())
    assert [m for m, _ in calls] == ["POST", "GET"]
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        assert (await session.get(BudgetReservationRow, receipt.reservation_id)).state == "held"


@pytest.mark.parametrize("pending", [True, False])
async def test_cancel_preserves_submitted_reconciliation(media_job, monkeypatch, pending):
    from aibroker.services import job_control, job_queue
    from tests.test_media_resubmit_risk import mock_transport

    calls = mock_transport(monkeypatch, "known_id_poll_failure")
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    if pending:
        await job_queue._release_claim(1, media_job[2].claim)
    result = await job_control.cancel_job(1, 1)
    assert result.state == "cancel_requested"
    if not pending:
        import httpx

        from aibroker.providers import gemini_video

        original_send = gemini_video._send

        async def fail_get(method, url, *args):
            calls.append((method, url))
            assert method == "GET"
            raise httpx.ReadTimeout("transient GET failure after cancellation")

        monkeypatch.setattr(gemini_video, "_send", fail_get)
        with pytest.raises(MediaUnknown):
            await run_media(media_job)
        monkeypatch.setattr(gemini_video, "_send", original_send)
        await job_queue._requeue_or_fail(1, 0, "GET unavailable", media_job[2].claim)
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        assert job.status == "pending" and job.cancel_requested
    await new_claim()
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
    await job_queue._execute_guarded(job)
    assert [m for m, _ in calls] == ["POST"] + ["GET"] * (2 if pending else 3)
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        hold = await session.get(BudgetReservationRow, receipt.reservation_id)
        assert receipt.phase == hold.state == "settled"
        assert receipt.usage_id == hold.usage_id
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1
        assert (await session.get(DeepJobRow, 1)).status == "done"


@pytest.mark.parametrize("revocation", ["scope", "inactive"])
async def test_revocation_during_booking_still_settles(media_job, monkeypatch, revocation):
    from tests.test_media_resubmit_risk import mock_transport

    calls = mock_transport(monkeypatch, "none")
    original = MediaDispatch.booked

    async def revoke_then_booked(self, cost):
        async with get_session() as session:
            project = await session.get(ProjectRow, 1)
            if revocation == "scope":
                project.allowed_scopes = []
            else:
                project.is_active = False
        return await original(self, cost)

    monkeypatch.setattr(MediaDispatch, "booked", revoke_then_booked)
    result = await run_media(media_job)
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        hold = await session.get(BudgetReservationRow, receipt.reservation_id)
        assert receipt.phase == hold.state == "settled"
        assert receipt.usage_id == hold.usage_id == result.request_id
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1
    for context in (MediaDispatch(1, 2, media_job[2].claim),
                    MediaDispatch(1, 1, media_job[2].claim - timedelta(seconds=1))):
        context.bind(media_job[1], hold.id, receipt.model)
        with pytest.raises(MediaUnknown):
            async with get_session() as session:
                await context.finalize(session, result.request_id)
    with pytest.raises(MediaUnknown):
        await run_media(media_job, await new_claim())
    assert [m for m, _ in calls] == ["POST"]


async def test_revocation_after_post_persists_receipt_but_stops_get(media_job, monkeypatch):
    from aibroker.providers import gemini_video
    from tests.test_media_resubmit_risk import mock_transport

    calls = mock_transport(monkeypatch, "known_id_poll_failure")
    original = gemini_video._send

    async def revoke_after_response(*args):
        response = await original(*args)
        async with get_session() as session:
            (await session.get(ProjectRow, 1)).allowed_scopes = []
        return response

    monkeypatch.setattr(gemini_video, "_send", revoke_after_response)
    with pytest.raises(MediaUnknown):
        await run_media(media_job)
    assert [m for m, _ in calls] == ["POST"]
    async with get_session() as session:
        receipt = await session.get(MediaDispatchRow, 1)
        assert receipt.provider_id == "known-receipt" and receipt.phase == "submitted"
        assert (await session.get(BudgetReservationRow, receipt.reservation_id)).state == "held"
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0


async def test_provider_responses_are_kept_capped_and_summarised(media_job):
    from aibroker.services.media_dispatch import (
        PROVIDER_BODY_MAX,
        PROVIDER_RESPONSES_MAX,
        UNKNOWN_OUTCOME,
        media_quarantine,
        note_provider_response,
    )

    for i in range(PROVIDER_RESPONSES_MAX + 2):
        await note_provider_response(1, method="POST", path="/v3/models/videos", status=403,
                                     body=f"call {i} " + "x" * (PROVIDER_BODY_MAX * 2))
    async with get_session() as session:
        meta = (await session.get(DeepJobRow, 1)).result_meta
    kept = meta["provider_responses"]
    assert len(kept) == PROVIDER_RESPONSES_MAX and kept[-1]["body"].startswith("call 6")
    assert all(len(r["body"]) <= PROVIDER_BODY_MAX for r in kept)
    async with get_session() as session:
        session.add(MediaDispatchRow(job_id=1, project_id=1, fingerprint="f", phase="submitting",
                                     claim=media_job[2].claim))
    assert await media_quarantine(1, media_job[2].claim)
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
    assert job.error_message.startswith(UNKNOWN_OUTCOME)
    assert "last provider response: POST /v3/models/videos HTTP 403" in job.error_message


async def test_resolve_unknown_without_submission_releases_the_hold(media_job):
    from aibroker.services.media_dispatch import (
        RESOLVED_NO_SUBMISSION,
        resolve_unknown_without_submission,
    )

    today = datetime.now(UTC).date()
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        job.status, job.error_message = "error", "media outcome unknown; automatic resubmit prohibited"
        session.add(BudgetReservationRow(id="hold-1", api_key_id=1, project_id=1, amount=0.48,
                                         day=today, month=today.replace(day=1), debit_daily=False,
                                         debit_monthly=False, state="held"))
        session.add(MediaDispatchRow(job_id=1, project_id=1, fingerprint="f", phase="unknown",
                                     claim=media_job[2].claim, key_id=1, reservation_id="hold-1",
                                     model="gemini/x"))
    result = await resolve_unknown_without_submission(1, 1, "provider list shows no video")
    assert result["reservation_state"] == "released"
    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        hold = await session.get(BudgetReservationRow, "hold-1")
    assert job.error_message.startswith(RESOLVED_NO_SUBMISSION) and hold.state == "released"
    with pytest.raises(MediaUnknown):            # wrong project
        await resolve_unknown_without_submission(1, 2, "x")


async def test_resolve_refuses_a_job_with_a_provider_receipt(media_job):
    from aibroker.services.media_dispatch import resolve_unknown_without_submission

    async with get_session() as session:
        job = await session.get(DeepJobRow, 1)
        job.status = "error"
        session.add(MediaDispatchRow(job_id=1, project_id=1, fingerprint="f", phase="unknown",
                                     claim=media_job[2].claim, provider_id="clip-9"))
    with pytest.raises(MediaUnknown, match="without a provider receipt"):
        await resolve_unknown_without_submission(1, 1, "x")
