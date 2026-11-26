"""Video 1's documented wire contract, durable retry and private CDN boundaries."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aibroker.providers import heygen_video as hg
from aibroker.providers.gemini_video import VideoRequest
from aibroker.providers.provider_specs.heygen import HEYGEN_MODEL
from aibroker.services.media_dispatch import MediaUnknown
from aibroker.services.video_service import video_job_payload, video_request_from_job

MP4 = b"\x00\x00\x00\x18ftypmp42"
REQ = VideoRequest('Speak: "Hello". Quiet room tone.', 5, "9:16", "768p",
                   b"image", "image/jpeg", "heygen")


@pytest.mark.parametrize("recovery", [False, True])
async def test_heygen_clip_reuses_receipt_without_leaking_key(monkeypatch, recovery):
    calls = []

    def wire(req):
        calls.append(req)
        if req.method == "POST":
            assert req.headers["Idempotency-Key"]
            body = json.loads(req.content)
            assert body["mode"] == "reference_to_video"
            assert "<Picture 1>" in body["prompt"]
            assert body["reference_images"] == [
                {"type": "base64", "data": "aW1hZ2U=", "media_type": "image/jpeg"}]
            return httpx.Response(202, json={"data": {"video_id": "clip-1", "status": "pending"}})
        if req.url.host == "api.heygen.com":
            assert req.headers["x-api-key"] == "synthetic"
            assert req.url.path.endswith("/clip-1")
            return httpx.Response(200, json={"data": {"video_id": "clip-1", "status": "completed",
                                                      "video_url": "https://resource2.heygen.ai/v.mp4",
                                                      "duration": 5}})
        assert "x-api-key" not in req.headers
        return httpx.Response(200, content=MP4)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    monkeypatch.setattr(hg.httpx, "AsyncClient", lambda **_kw: client)
    monkeypatch.setattr(hg, "_POLL_S", 0)
    dispatch = SimpleNamespace(
        job_id=7, load=AsyncMock(return_value={
            "phase": "submitted" if recovery else "prepared", "provider_id": "clip-1",
            "fingerprint": "fixture",
        }), begin=AsyncMock(), receipt=AsyncMock(), authorize_http=AsyncMock(),
    )
    result, meta = await hg.HeyGenVideoTransport().generate_video(
        model=HEYGEN_MODEL, request=REQ, api_key="synthetic", dispatch=dispatch)
    assert result == MP4 and meta["cost_usd"] == pytest.approx(0.30)
    assert [r.method for r in calls] == (["GET", "GET"] if recovery else ["POST", "GET", "GET"])
    assert dispatch.begin.await_count == dispatch.receipt.await_count == (0 if recovery else 1)
    assert dispatch.authorize_http.await_count == 2


@pytest.mark.parametrize("url", ["http://resource2.heygen.ai/v.mp4",
                                  "https://resource2.heygen.ai.invalid/v.mp4",
                                  "https://resource2.heygen.ai@127.0.0.1/video"])
async def test_refuses_untrusted_media_url_without_an_http_request(url):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected network download"))
    ) as client:
        with pytest.raises(RuntimeError, match="host rejected"):
            await hg._download(client, url)


async def test_no_post_if_submit_outcome_unknown(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: pytest.fail("unexpected retry")))
    monkeypatch.setattr(hg.httpx, "AsyncClient", lambda **_: client)
    dispatch = SimpleNamespace(load=AsyncMock(return_value={"phase": "unknown", "provider_id": None}))
    with pytest.raises(MediaUnknown, match="no resubmit"):
        await hg.HeyGenVideoTransport().generate_video(
            model=HEYGEN_MODEL, request=REQ, api_key="synthetic", dispatch=dispatch)


def test_provider_survives_job_serialization_without_invalidating_older_jobs():
    assert video_request_from_job(video_job_payload(REQ)) == REQ
    old = VideoRequest("hello", 5, "9:16", "720p")
    assert "provider" not in video_job_payload(old)


def _submit_dispatch():
    return SimpleNamespace(job_id=9, load=AsyncMock(return_value={
        "phase": "prepared", "provider_id": None, "fingerprint": "fixture"}),
        begin=AsyncMock(), receipt=AsyncMock(), authorize_http=AsyncMock())


@pytest.mark.parametrize(("response", "error"), [
    (httpx.Response(403, text='{"error": "insufficient wallet balance"}'), RuntimeError),
    (httpx.Response(200, text="<html>maintenance</html>"), RuntimeError),
    (httpx.Response(202, json={"data": {"status": "pending"}}), MediaUnknown),
])
async def test_failed_or_unexpected_submit_answers_are_kept_without_the_key(
        monkeypatch, response, error):
    noted = AsyncMock()
    monkeypatch.setattr("aibroker.services.media_dispatch.note_provider_response", noted)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: response))
    monkeypatch.setattr(hg.httpx, "AsyncClient", lambda **_kw: client)
    with pytest.raises(error):
        await hg.HeyGenVideoTransport().generate_video(
            model=HEYGEN_MODEL, request=REQ, api_key="synthetic-secret",
            dispatch=_submit_dispatch())
    kw = noted.await_args.kwargs
    assert noted.await_args.args == (9,)
    assert kw["method"] == "POST" and kw["path"] == "/v3/models/videos"
    assert kw["status"] == (None if error is MediaUnknown else response.status_code)
    assert "synthetic-secret" not in kw["body"]
    assert kw.get("secret") in (None, "synthetic-secret")   # only to cut it out verbatim
    assert kw["body"]


async def test_a_failed_poll_answer_is_kept(monkeypatch):
    noted = AsyncMock()
    monkeypatch.setattr("aibroker.services.media_dispatch.note_provider_response", noted)
    monkeypatch.setattr(hg, "_POLL_S", 0)

    def wire(req):
        if req.method == "POST":
            return httpx.Response(202, json={"data": {"video_id": "clip-2", "status": "pending"}})
        return httpx.Response(500, text="upstream exploded")

    client = httpx.AsyncClient(transport=httpx.MockTransport(wire))
    monkeypatch.setattr(hg.httpx, "AsyncClient", lambda **_kw: client)
    with pytest.raises(RuntimeError):
        await hg.HeyGenVideoTransport().generate_video(
            model=HEYGEN_MODEL, request=REQ, api_key="synthetic", dispatch=_submit_dispatch())
    kw = noted.await_args.kwargs
    assert (kw["method"], kw["path"], kw["status"]) == ("GET", "/v3/models/videos/clip-2", 500)
    assert kw["body"] == "upstream exploded"


async def test_an_accepted_video_id_is_kept_when_storing_the_receipt_fails(monkeypatch):
    noted = AsyncMock()
    monkeypatch.setattr("aibroker.services.media_dispatch.note_provider_response", noted)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _req: httpx.Response(
        202, json={"data": {"video_id": "clip-3", "status": "pending"}})))
    monkeypatch.setattr(hg.httpx, "AsyncClient", lambda **_kw: client)
    dispatch = _submit_dispatch()
    dispatch.receipt = AsyncMock(side_effect=MediaUnknown("stale claim"))
    with pytest.raises(MediaUnknown):
        await hg.HeyGenVideoTransport().generate_video(
            model=HEYGEN_MODEL, request=REQ, api_key="synthetic", dispatch=dispatch)
    assert "video_id=clip-3" in noted.await_args.kwargs["body"]
