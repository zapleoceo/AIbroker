"""Video lane: services/video_service.py, POST /v1/video/jobs and the job poll shape.

Paid-only key choice, honest cost reservation, rotation, caps, refusals, and the
route (scope, validation, enqueue). No real provider is called.
"""
from __future__ import annotations

import base64
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import aibroker.services.video_service as svc
from aibroker.main import app
from aibroker.providers.gemini_video import VideoRejected, VideoRequest
from aibroker.routes import proxy
from aibroker.routing.cost_guard import CostGuardError
from aibroker.services.llm_service import _BudgetExhausted
from tests.test_routes_proxy import _make_project

client = TestClient(app)
PROJECT = SimpleNamespace(id=1, name="sim")
MODEL = "gemini/gemini-omni-1.2-flash"
MP4 = b"\x00\x00\x00\x18ftypmp42"
REQ = VideoRequest(prompt="A woman waves", duration_s=5, aspect_ratio="9:16",
                   resolution="360p")


def _meta(cost=0.116):
    return {"model": MODEL, "model_served": None, "mime_type": "video/mp4",
            "tokens_in": 20, "tokens_out": 9_655, "cost_usd": cost, "video_s": 5.0,
            "latency_ms": 42_000}


def _paid_key(kid=1):
    return SimpleNamespace(id=kid, label=f"paid{kid}", tier="paid",
                           provider="gemini", token_encrypted="x")


def _wire(monkeypatch, *, picks, gen, record=None, reserve=None):
    calls = {"pick": []}

    async def fake_pick(provider, scope, **kw):
        calls["pick"].append({"provider": provider, "scope": scope, **kw})
        i = len(calls["pick"]) - 1
        return picks[i] if i < len(picks) else None

    note = AsyncMock(return_value=None)
    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "generate_video", gen)
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost",
                        reserve or AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.release_cost", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt._penalize", AsyncMock(return_value="error"))
    monkeypatch.setattr("aibroker.services.attempt._record_error", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.affinity.note_success", note)
    monkeypatch.setattr("aibroker.services.attempt.record_usage",
                        record or AsyncMock(return_value=77))
    calls["note"] = note
    return calls


async def _run(**kw):
    args = {"project": PROJECT, "request": REQ, "model": None, "workflow": "kruzhok"} | kw
    return await svc.run_video(**args)


# ─── service ─────────────────────────────────────────────────────────────


async def test_only_paid_keys_with_the_video_scope_are_asked_for(monkeypatch):
    calls = _wire(monkeypatch, picks=[_paid_key()],
                  gen=AsyncMock(return_value=(MP4, _meta())))
    await _run()
    pick = calls["pick"][0]
    assert (pick["provider"], pick["scope"], pick["require_tier"]) == \
        ("gemini", "llm:video", "paid")
    assert pick["models"] == [MODEL]


async def test_success_returns_the_clip_and_books_cost(monkeypatch):
    record = AsyncMock(return_value=77)
    gen = AsyncMock(return_value=(MP4, _meta(cost=0.116)))
    calls = _wire(monkeypatch, picks=[_paid_key()], gen=gen, record=record)
    out = await _run()
    assert base64.b64decode(out.text) == MP4 and out.mime_type == "video/mp4"
    assert out.request_id == 77 and out.key_label == "paid1" and out.video_s == 5.0
    assert gen.await_args.kwargs == {"model": MODEL, "request": REQ, "api_key": "plain"}
    kw = record.await_args.kwargs
    assert (kw["capability"], kw["status"], kw["tokens_out"]) == ("video", "ok", 9_655)
    assert kw["cost_usd"] == pytest.approx(0.116)
    calls["note"].assert_not_awaited()    # a clip has no prompt cache to stay warm for


async def test_reservation_is_the_estimated_clip_price(monkeypatch):
    reserve = AsyncMock(return_value=None)
    _wire(monkeypatch, picks=[_paid_key()],
          gen=AsyncMock(return_value=(MP4, _meta())), reserve=reserve)
    await _run()
    assert reserve.await_args.kwargs["estimated_cost"] == pytest.approx(
        svc.estimate_video_cost(MODEL, REQ))
    assert reserve.await_args.kwargs["estimated_cost"] > 0.1


async def test_pinned_model_is_resolved_through_the_catalog(monkeypatch):
    calls = _wire(monkeypatch, picks=[_paid_key()],
                  gen=AsyncMock(return_value=(MP4, _meta())))
    out = await _run(model="gemini-omni-1.2-flash")
    assert out.model == MODEL and calls["pick"][0]["models"] == [MODEL]


async def test_rotates_to_the_next_paid_key_on_failure(monkeypatch):
    n = {"i": 0}

    async def flaky(**kw):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("gemini-video 503 (the create call): overloaded")
        return MP4, _meta()
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], gen=flaky)
    assert (await _run()).key_label == "paid2"


async def test_every_key_failing_raises_video_failed(monkeypatch):
    async def dead(**kw):
        raise RuntimeError("boom")
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], gen=dead)
    with pytest.raises(svc.VideoFailed, match="boom"):
        await _run()


async def test_a_refused_prompt_stops_the_walk_at_the_first_key(monkeypatch):
    gen = AsyncMock(side_effect=VideoRejected("gemini-video HTTP 400: blocked"))
    calls = _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], gen=gen)
    with pytest.raises(VideoRejected, match="blocked"):
        await _run()
    assert len(calls["pick"]) == 1 and gen.await_count == 1


async def test_no_paid_key_returns_none(monkeypatch):
    _wire(monkeypatch, picks=[], gen=AsyncMock())
    assert await _run() is None


async def test_spent_project_cap_is_reported_without_calling_the_provider(monkeypatch):
    provider = AsyncMock()

    async def blocked(**kw):
        raise CostGuardError("project", 1.0, 1.0, 0.1)
    _wire(monkeypatch, picks=[_paid_key()], gen=provider, reserve=blocked)
    assert isinstance(await _run(), _BudgetExhausted)
    provider.assert_not_awaited()


def test_job_payload_round_trips_with_and_without_an_image():
    assert svc.video_request_from_job(svc.video_job_payload(REQ)) == REQ
    with_image = VideoRequest(prompt="p", duration_s=3, aspect_ratio="16:9",
                              resolution="720p", image=b"\xff\xd8jpeg", image_mime="image/jpeg")
    payload = svc.video_job_payload(with_image)
    assert isinstance(payload["image_b64"], str)
    assert svc.video_request_from_job(payload) == with_image
    with pytest.raises(KeyError):
        svc.video_request_from_job({"prompt": "p"})


def test_a_video_job_is_retried_far_less_than_a_chat_job():
    from aibroker.services import job_queue
    assert svc.VIDEO_MAX_RETRIES < job_queue._MAX_RETRIES
    assert job_queue.VIDEO_FIELD == "video"


# ─── route ───────────────────────────────────────────────────────────────


def _post(plain, body):
    return client.post("/v1/video/jobs", headers={"X-Project-Key": plain}, json=body)


async def test_route_requires_the_video_scope():
    plain, _ = await _make_project(["llm:chat", "llm:speech"])
    with patch("aibroker.routes.proxy.submit_job", AsyncMock()) as submit:
        r = _post(plain, {"prompt": "hi"})
    assert r.status_code == 403 and "llm:video" in r.json()["detail"]
    submit.assert_not_awaited()


@pytest.mark.parametrize("body", [
    {"prompt": ""}, {"prompt": "   "}, {"prompt": "x" * 4001},
    {"prompt": "hi", "duration_s": 2}, {"prompt": "hi", "duration_s": 16},
    {"prompt": "hi", "aspect_ratio": "1:1"}, {"prompt": "hi", "resolution": "4k"},
    {"prompt": "hi", "image_base64": "aGk=", "image_mime": "image/gif"},
])
async def test_route_rejects_invalid_requests_with_422(body):
    plain, _ = await _make_project(["llm:video"])
    with patch("aibroker.routes.proxy.submit_job", AsyncMock()) as submit:
        r = _post(plain, body)
    assert r.status_code == 422
    submit.assert_not_awaited()


@pytest.mark.parametrize("body", [
    {"prompt": "hi", "model": "gemini-2.5-flash"},
    {"prompt": "hi", "image_base64": "not base64 !!"},
    {"prompt": "hi", "image_base64": base64.b64encode(
        b"x" * (proxy.VIDEO_MAX_IMAGE_BYTES + 1)).decode()},
])
async def test_route_rejects_a_wrong_model_or_image_with_400(body):
    plain, _ = await _make_project(["llm:video"])
    with patch("aibroker.routes.proxy.submit_job", AsyncMock()) as submit:
        r = _post(plain, body)
    assert r.status_code == 400
    submit.assert_not_awaited()


async def test_route_enqueues_a_video_job_and_returns_its_poll_url():
    plain, _ = await _make_project(["llm:video"])
    image = base64.b64encode(b"\xff\xd8jpeg").decode()
    with patch("aibroker.routes.proxy.submit_job", AsyncMock(return_value=512)) as submit:
        r = _post(plain, {"prompt": "Она машет рукой", "duration_s": 6,
                          "resolution": "360p", "image_base64": image,
                          "workflow": "kruzhok"})
    assert r.status_code == 202
    assert r.json() == {"job_id": 512, "status": "pending", "poll_url": "/v1/jobs/512",
                        "poll_after_s": 10, "trace_id": None, "labels": None}
    kw = submit.await_args.kwargs
    assert (kw["capability"], kw["model"], kw["workflow"]) == ("video", None, "kruzhok")
    assert kw["extra"] == {"video": {
        "prompt": "Она машет рукой", "duration_s": 6, "aspect_ratio": "9:16",
        "resolution": "360p", "image_b64": image, "image_mime": "image/jpeg"}}


def _row(capability: str, text: str):
    now = datetime(2026, 11, 14, 12, 0, 0)
    return SimpleNamespace(
        id=9, status="done", capability=capability, request={}, result_text=text,
        result_meta={"provider": "gemini", "model": MODEL, "cost_usd": 0.116,
                     "mime_type": "video/mp4" if capability == "video" else None,
                     "video_s": 5.0 if capability == "video" else None},
        error_message=None, trace_id=None, labels=None, priority=None,
        created_at=now, started_at=now, completed_at=now, run_after=None)


def test_poll_returns_a_finished_clip_as_video_base64_not_text():
    clip = base64.b64encode(MP4).decode()
    done = proxy._job_response(_row("video", clip))
    assert done.status == "done" and done.text is None
    assert (done.video_base64, done.mime_type, done.video_s) == (clip, "video/mp4", 5.0)
    assert done.cost_usd == pytest.approx(0.116)
    chat = proxy._job_response(_row("chat:fast", "hello"))
    assert chat.text == "hello" and chat.video_base64 is None and chat.mime_type is None
