"""Queue speech fences with isolated database and fake transport; no provider calls."""
import base64
import io
import wave
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import ApiKeyRow, DeepJobFileRow, DeepJobRow, ProjectRow, UsageLogRow
from aibroker.main import app
from aibroker.providers import gemini_tts
from aibroker.services import job_queue, speech_jobs
from aibroker.services.media_dispatch import MediaUnknown
from tests.test_routes_proxy import _make_project


def _make_wav(*, rate: int = 24_000, channels: int = 1, width: int = 2) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(width)
        audio.setframerate(rate)
        audio.writeframes(b"\x01" * (4 * channels * width))
    return out.getvalue()


_TEST_WAV = _make_wav()


@pytest.fixture
async def speech_job(monkeypatch):
    monkeypatch.setattr(get_settings(), "GLOBAL_DAILY_CAP_USD", 100)
    monkeypatch.setattr(speech_jobs, "decrypt", lambda _: "FAKE")
    start = datetime.now(UTC).replace(tzinfo=None)
    async with get_session() as s:
        s.add(ProjectRow(id=1, name="speech-fixture", project_key_hash="h",
                         project_key_prefix="p", allowed_scopes=["llm:speech"],
                         daily_cost_cap_usd=10, monthly_cost_cap_usd=10))
        await s.flush()
        s.add(ApiKeyRow(id=1, provider="gemini", label="speech-fixture", tier="paid",
                        token_encrypted="FAKE", account_id="fixture-account", scopes=["llm:speech"],
                        daily_cost_cap_usd=10, is_active=True, is_alive=True))
        s.add(DeepJobRow(id=1, project_id=1, capability="speech", status="running", started_at=start,
                         request={"speech_text": "synthetic", "speech_voice": "Kore",
                                  "speech_style": None, "model": None}))
    async with get_session() as s:
        row, key = await s.get(DeepJobRow, 1), await s.get(ApiKeyRow, 1)
    picker = AsyncMock(return_value=key)
    monkeypatch.setattr(speech_jobs, "pick_and_reserve", picker)
    return row, picker


async def test_speech_fence_exists_before_transport_and_success_is_booked(speech_job, monkeypatch):
    row, picker = speech_job

    async def fake_tts(**_):
        async with get_session() as s:
            receipt = await s.get(MediaDispatchRow, 1)
            assert receipt.phase == "submitting"
            assert (await s.get(BudgetReservationRow, receipt.reservation_id)).state == "held"
        return _TEST_WAV, {"mime_type": "audio/wav", "cost_usd": 0.01, "latency_ms": 10}

    tts = AsyncMock(side_effect=fake_tts)
    monkeypatch.setattr(speech_jobs, "synthesize_via_gemini", tts)
    await job_queue._execute(row)
    await job_queue._execute(row)  # stale notification cannot dispatch again
    tts.assert_awaited_once()
    picker.assert_awaited_once()
    async with get_session() as s:
        receipt = await s.get(MediaDispatchRow, 1)
        stored = await s.get(DeepJobFileRow, (1, 0))
        assert receipt.phase == "settled"
        assert (await s.get(BudgetReservationRow, receipt.reservation_id)).state == "settled"
        assert (await s.get(UsageLogRow, receipt.usage_id)).cost_usd == pytest.approx(0.01)
        assert (await s.get(DeepJobRow, 1)).status == "done"
        assert (await s.get(DeepJobRow, 1)).request == {}
        assert stored.data == _TEST_WAV and stored.expires_at > row.started_at


async def test_speech_timeout_no_rotation_no_two_calls_and_hold_retained(speech_job, monkeypatch):
    row, picker = speech_job
    tts = AsyncMock(side_effect=TimeoutError("private provider detail"))
    monkeypatch.setattr(speech_jobs, "synthesize_via_gemini", tts)
    await job_queue._execute(row)
    await job_queue._execute(row)
    tts.assert_awaited_once()
    picker.assert_awaited_once()
    async with get_session() as s:
        receipt = await s.get(MediaDispatchRow, 1)
        recovered = await s.get(DeepJobRow, 1)
        assert receipt.phase == "unknown" and receipt.usage_id is None
        assert (await s.get(BudgetReservationRow, receipt.reservation_id)).state == "held"
        assert recovered.status == "error" and recovered.error_message == "speech_outcome_unknown"
        assert recovered.request == {}
        assert await s.get(DeepJobFileRow, (1, 0)) is None


async def test_cancel_before_speech_dispatch_clears_inputs(speech_job, monkeypatch):
    row, picker = speech_job
    tts = AsyncMock(side_effect=AssertionError("must not call provider"))
    monkeypatch.setattr(speech_jobs, "synthesize_via_gemini", tts)
    async with get_session() as s:
        (await s.get(DeepJobRow, 1)).cancel_requested = True
    row.cancel_requested = True
    await job_queue._execute(row)
    tts.assert_not_called()
    picker.assert_not_called()
    async with get_session() as s:
        cancelled = await s.get(DeepJobRow, 1)
        assert cancelled.status == "cancelled" and cancelled.request == {}
        assert await s.get(MediaDispatchRow, 1) is None


def _tts_key(now: datetime) -> str:
    return f"tts-{int(now.timestamp() * 1000)}-{uuid4()}"


async def test_typed_speech_admission_replay_and_binary_are_private(monkeypatch):
    config = get_settings()
    monkeypatch.setattr(config, "ASYNC_SPEECH_ADMISSION_ENABLED", False)
    key, pid = await _make_project(["llm:speech"])
    other, _ = await _make_project(["llm:speech"])
    denied, _ = await _make_project(["llm:chat"])
    client = TestClient(app)
    body = {"text": "synthetic queue"}
    idem = _tts_key(datetime.now(UTC))

    def post(headers, payload=body):
        return client.post("/v1/speech/jobs", headers={"Idempotency-Key": idem, **headers},
                           json=payload)

    own = {"X-Project-Key": key}
    assert post(own).status_code == 503
    monkeypatch.setattr(config, "ASYNC_SPEECH_ADMISSION_ENABLED", True)
    assert post({"X-Project-Key": denied}).status_code == 403
    expired = _tts_key(datetime.now(UTC) - timedelta(days=2))
    assert post({**own, "Idempotency-Key": expired}).status_code == 422
    async with get_session() as s:
        visible = DeepJobRow(project_id=pid, capability="chat:fast", status="done",
                             request={}, labels={"isolation": "scope"}, trace_id="isolation")
        s.add(visible)
        await s.flush()
        visible_id = visible.id
    accepted = post(own)
    assert accepted.status_code == 202, accepted.text
    receipt = accepted.json()
    status_url = receipt["status_url"]
    result_url = status_url + "/result"
    again = post(own)
    assert again.status_code == 200 and again.headers["Idempotent-Replay"] == "true"
    assert again.json()["job_id"] == receipt["job_id"]
    assert post(own, {"text": "changed"}).status_code == 409
    assert client.get(status_url, headers={"X-Project-Key": other}).status_code == 404
    assert client.get(result_url, headers={"X-Project-Key": denied}).status_code == 403
    assert client.get(result_url, headers=own).status_code == 409
    lookup = f"/v1/speech/jobs/by-key/{idem}"
    recovered = client.get(lookup, headers=own)
    assert recovered.status_code == 200 and recovered.json()["job_id"] == receipt["job_id"]
    assert recovered.headers["cache-control"] == "private, no-store"
    assert client.get(lookup, headers={"X-Project-Key": other}).status_code == 404
    assert client.get(lookup, headers={"X-Project-Key": denied}).status_code == 403
    async with get_session() as s:
        speech = await s.get(DeepJobRow, receipt["job_id"])
        speech.labels = {"isolation": "scope", "speech_only": "yes"}
        speech.trace_id = "isolation"
    page = client.get("/v1/jobs?label.isolation=scope&limit=1", headers=own)
    assert page.status_code == 200
    result = page.json()
    assert [j["job_id"] for j in result["jobs"]] == [visible_id]
    assert result["high_watermark"] == visible_id
    assert result["no_nonterminal_in_filter"] and result["pagination_complete"]
    assert result["next_cursor"] is None
    no_rows = client.get("/v1/jobs?label.speech_only=yes", headers=own).json()
    assert no_rows["jobs"] == [] and no_rows["no_nonterminal_in_filter"]
    assert no_rows["high_watermark"] == visible_id
    assert client.post(status_url.replace("/speech", "") + "/cancel", headers=own).status_code == 404
    for selector in ({"labels": {"speech_only": "yes"}}, {"trace_id": "isolation"}):
        bulk = client.post("/v1/jobs/cancel", headers=own, json=selector)
        assert bulk.status_code == 200
        assert bulk.json() == {"cancelled": [], "cancel_requested": [], "already_terminal": 0}
    assert client.get(lookup, headers=own).json()["status"] == "pending"
    for route in (f"/v1/jobs/{receipt['job_id']}", f"/v1/deep/{receipt['job_id']}"):
        assert client.get(route, headers=own).status_code == 404

    # Existing receipt remains readable with admission off. Store only fake bytes.
    monkeypatch.setattr(config, "ASYNC_SPEECH_ADMISSION_ENABLED", False)
    job_id = receipt["job_id"]
    async with get_session() as s:
        row = await s.get(DeepJobRow, job_id)
        assert (row.project_id, row.capability, row.status) == (pid, "speech", "pending")
        row.status = "done"
        s.add(DeepJobFileRow(job_id=job_id, idx=0, sha256="0" * 64, mime="audio/wav",
                             data=_TEST_WAV, size=len(_TEST_WAV),
                             expires_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=1)))
    assert client.get(status_url, headers=own).json()["result_url"] == result_url
    assert client.get(result_url, headers={"X-Project-Key": other}).status_code == 404
    result = client.get(result_url, headers=own)
    assert result.status_code == 200 and result.content == _TEST_WAV
    assert result.headers["cache-control"] == "private, no-store"
    assert result.headers["content-type"] == "audio/wav"
    async with get_session() as s:
        (await s.get(ProjectRow, pid)).allowed_scopes = ["llm:chat"]
    assert client.get(status_url, headers=own).status_code == 403
    assert client.get(lookup, headers=own).status_code == 403
    assert client.get(result_url, headers=own).status_code == 403
    for route in (f"/v1/jobs/{job_id}", f"/v1/deep/{job_id}"):
        assert client.get(route, headers=own).status_code == 404
    async with get_session() as s:
        (await s.get(ProjectRow, pid)).allowed_scopes = ["llm:speech"]
    async with get_session() as s:
        file = await s.get(DeepJobFileRow, (job_id, 0))
        file.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    assert client.get(result_url, headers=own).status_code == 410
    await speech_jobs._purge_speech_audio()
    async with get_session() as s:
        assert await s.get(DeepJobFileRow, (job_id, 0)) is None


async def _prepare_fixture(speech_job):
    row, _ = speech_job
    async with get_session() as s:
        project, key = await s.get(ProjectRow, 1), await s.get(ApiKeyRow, 1)
    fence = speech_jobs._SpeechDispatch(row, row.started_at, key, "fixture-speech-model")
    await fence.prepare(project, 0.01)
    return row, fence


async def test_stop_stale_speech_after_fence_does_not_requeue_or_refund(speech_job):
    row, fence = await _prepare_fixture(speech_job)
    await fence.begin()
    assert await job_queue._release_claim(row.id, row.started_at)
    async with get_session() as s:
        assert (await s.get(DeepJobRow, 1)).error_message == "speech_outcome_unknown"
        assert (await s.get(MediaDispatchRow, 1)).phase == "unknown"
        assert (await s.get(BudgetReservationRow, fence.reservation_id)).state == "held"


async def test_lost_worker_after_prepare_before_fence_refunds_once(speech_job):
    row, fence = await _prepare_fixture(speech_job)
    async with get_session() as s:
        receipt = await s.get(MediaDispatchRow, 1)
        assert receipt.phase == "prepared" and receipt.reservation_id == fence.reservation_id
        hold = await s.get(BudgetReservationRow, fence.reservation_id)
        assert hold.state == "held"
        assert (await s.get(ApiKeyRow, 1)).daily_cost_used_usd == pytest.approx(0.01)
    assert await speech_jobs._stop_claim(row.id, row.started_at)
    assert not await speech_jobs._stop_claim(row.id, row.started_at)
    with pytest.raises(MediaUnknown, match="stale speech claim"):
        await fence.begin()
    async with get_session() as s:
        assert (await s.get(BudgetReservationRow, fence.reservation_id)).state == "released"
        assert (await s.get(ApiKeyRow, 1)).daily_cost_used_usd == pytest.approx(0)
        terminal = await s.get(DeepJobRow, 1)
        assert terminal.status == "error" and terminal.error_message == "speech_worker_unavailable"
        assert await s.scalar(select(func.count()).select_from(UsageLogRow)) == 0


async def test_failure_between_debit_and_prepared_record_rolls_back_both(speech_job, monkeypatch):
    reserve = speech_jobs._admit_ordinary_locked

    async def break_before_receipt(s, *args):
        rid = await reserve(s, *args)
        assert (await s.get(BudgetReservationRow, rid)).state == "held"
        raise RuntimeError("synthetic crash before receipt insertion")

    monkeypatch.setattr(speech_jobs, "_admit_ordinary_locked", break_before_receipt)
    row, _ = speech_job
    await job_queue._execute(row)
    async with get_session() as s:
        assert await s.scalar(select(func.count()).select_from(BudgetReservationRow)) == 0
        assert await s.get(MediaDispatchRow, 1) is None
        assert (await s.get(ApiKeyRow, 1)).daily_cost_used_usd == pytest.approx(0)
        assert (await s.get(DeepJobRow, 1)).status == "error"


@pytest.mark.parametrize(("data", "mime", "served"), [
    (b"RIFF-not-a-wave", "audio/wav", None),
    (_TEST_WAV, "audio/l16", None),
    (_make_wav(rate=16_000), "audio/wav", None),
    (_make_wav(channels=2), "audio/wav", None),
    (_make_wav(width=3), "audio/wav", None),
    (_TEST_WAV, "audio/wav", "gemini-3.1-flash-tts-preview"),
    (b"", None, None),
])
async def test_known_paid_invalid_audio_is_billed_once_as_terminal_error(
    speech_job, monkeypatch, data, mime, served,
):
    row, picker = speech_job
    tts = AsyncMock(return_value=(data, {"mime_type": mime, "model_served": served,
                                        "cost_usd": 0.01, "latency_ms": 10}))
    monkeypatch.setattr(speech_jobs, "synthesize_via_gemini", tts)
    await job_queue._execute(row)
    await job_queue._execute(row)
    tts.assert_awaited_once()
    picker.assert_awaited_once()
    async with get_session() as s:
        job = await s.get(DeepJobRow, 1)
        receipt = await s.get(MediaDispatchRow, 1)
        usage = await s.get(UsageLogRow, receipt.usage_id)
        assert receipt.phase == "settled"
        assert (job.status, job.error_message) == ("error", "speech_result_unsupported")
        assert (usage.status, usage.error_kind) == ("error", "SpeechOutputInvalid")
        assert usage.cost_usd == pytest.approx(0.01)
        assert (await s.get(BudgetReservationRow, receipt.reservation_id)).state == "settled"
        assert await s.get(DeepJobFileRow, (1, 0)) is None


@pytest.mark.parametrize("output_tokens", [None, 0, 125])
async def test_valid_wav_needs_positive_output_usage_before_settlement(
    speech_job, monkeypatch, output_tokens,
):
    row, picker = speech_job
    usage = {"promptTokenCount": 4}
    if output_tokens is not None:
        usage["candidatesTokenCount"] = output_tokens
    response_json = {
        "candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/wav", "data": base64.b64encode(_TEST_WAV).decode(),
        }}]}}],
        "usageMetadata": usage,
    }
    post = AsyncMock(return_value=httpx.Response(200, json=response_json))
    monkeypatch.setattr(gemini_tts, "_post_bounded_tts", post)
    await job_queue._execute(row)
    await job_queue._execute(row)
    post.assert_awaited_once()
    picker.assert_awaited_once()
    async with get_session() as s:
        job = await s.get(DeepJobRow, 1)
        receipt = await s.get(MediaDispatchRow, 1)
        hold = await s.get(BudgetReservationRow, receipt.reservation_id)
        audio = await s.get(DeepJobFileRow, (1, 0))
        if output_tokens:
            assert (job.status, receipt.phase, hold.state) == ("done", "settled", "settled")
            used = await s.get(UsageLogRow, receipt.usage_id)
            assert used.tokens_out == output_tokens
            assert used.cost_usd == pytest.approx(gemini_tts.tts_cost(used.model, 4, output_tokens))
            assert audio.data == _TEST_WAV
        else:
            assert (job.status, receipt.phase, hold.state) == ("error", "unknown", "held")
            assert job.error_message == "speech_outcome_unknown" and audio is None
            assert receipt.usage_id is None
            assert await s.scalar(select(func.count()).select_from(UsageLogRow)) == 0
