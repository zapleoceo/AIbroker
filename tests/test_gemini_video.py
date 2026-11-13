"""Gemini Omni video transport (providers/gemini_video.py). No real provider is called."""
from __future__ import annotations

import base64
from types import SimpleNamespace

import httpx
import pytest

import aibroker.providers.gemini_video as gv
from aibroker.providers import transport as t
from aibroker.providers.catalog import UnknownModel, resolve_pin
from aibroker.providers.provider_errors import classify_provider_error
from aibroker.providers.registry import model_for, model_spec
from aibroker.routing.chains import CAPABILITY_CHAINS, scope_for, usable_scopes_for_provider

MODEL = "gemini/gemini-omni-1.2-flash"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
URI = "https://generativelanguage.googleapis.com/v1beta/files/abc123:download?alt=media"
REQ = gv.VideoRequest(prompt="A woman waves at the camera", duration_s=5,
                      aspect_ratio="9:16", resolution="360p")


def _interaction(part: dict | None, status: str = "completed", usage: dict | None = None) -> dict:
    content = [{"type": "thought", "text": "..."}] + ([part] if part else [])
    return {"id": "v1_x", "object": "interaction", "status": status,
            "model": "gemini-omni-1.2-flash",
            "steps": [{"type": "model_output", "content": content}],
            **({"usage": usage} if usage else {})}


def _script(*replies):
    """A fake `_send` answering the scripted replies in order, recording the calls."""
    calls: list[dict] = []
    queue = list(replies)

    async def fake_send(method, url, api_key, body, timeout):
        calls.append({"method": method, "url": url, "key": api_key, "body": body})
        reply = queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, payload = reply
        req = httpx.Request(method, url)
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload, request=req)
        if isinstance(payload, dict):
            return httpx.Response(status, json=payload, request=req)
        return httpx.Response(status, text=payload, request=req)
    return fake_send, calls


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(gv, "get_settings", lambda: SimpleNamespace(
        GEMINI_VIDEO_TIMEOUT_S=30.0, GEMINI_VIDEO_POLL_S=0.0))


async def _generate(monkeypatch, *replies, request=REQ):
    send, calls = _script(*replies)
    monkeypatch.setattr(gv, "_send", send)
    out = await gv.generate_via_gemini(model=MODEL, request=request, api_key="k")
    return out, calls


# ─── registry / routing wiring ───────────────────────────────────────────


def test_video_is_its_own_paid_gemini_lane():
    assert CAPABILITY_CHAINS["video"] == ["gemini"]
    assert scope_for("video") == "llm:video"
    assert "llm:video" in usable_scopes_for_provider("gemini")
    assert "llm:video" not in usable_scopes_for_provider("openai")


def test_video_lane_model_is_priced_and_uses_the_video_transport():
    assert model_for("gemini", "video") == MODEL
    spec = model_spec(MODEL)
    assert spec.transport == "gemini_video" and spec.pricing == "override"
    assert (spec.input_usd_per_mtok, spec.output_usd_per_mtok) == (1.50, 12.00)
    assert spec.capabilities == frozenset({"video"})


def test_transport_is_registered_and_implements_the_protocol():
    assert isinstance(t._load()["gemini_video"], t.VideoTransport)


def test_pin_resolves_by_bare_name_and_only_on_the_video_lane():
    assert [(x.provider, x.model) for x in
            resolve_pin("gemini-omni-1.2-flash", "video", ["gemini"])] == [("gemini", MODEL)]
    with pytest.raises(UnknownModel, match="does not serve"):
        resolve_pin("gemini-omni-1.2-flash", "chat:fast", ["gemini"])
    with pytest.raises(UnknownModel, match="does not serve"):
        resolve_pin("gemini-2.5-flash", "video", ["gemini"])


# ─── price ───────────────────────────────────────────────────────────────


def test_cost_follows_googles_tokens_per_second_table():
    # 5 s of 720p = 28 960 output tokens at $12 per 1M -> about $0.35.
    assert gv.video_output_tokens("720p", 5) == 28_960
    assert gv.video_cost(MODEL, 0, 28_960) == pytest.approx(0.34752)
    assert gv.video_cost(MODEL, 1_000_000, 0) == pytest.approx(1.50)
    assert gv.video_cost("gemini/unknown-video", 10, 10) == 0.0


def test_reservation_covers_the_clip_and_grows_with_a_reference_image():
    plain = gv.estimate_video_cost(MODEL, REQ)
    assert plain >= gv.video_cost(MODEL, 0, gv.video_output_tokens("360p", 5)) > 0
    with_image = gv.estimate_video_cost(MODEL, gv.VideoRequest(
        prompt=REQ.prompt, duration_s=5, aspect_ratio="9:16", resolution="360p",
        image=b"jpeg", image_mime="image/jpeg"))
    assert with_image > plain


# ─── request ─────────────────────────────────────────────────────────────


def test_body_asks_for_a_hosted_video_of_the_requested_shape():
    body = gv.interaction_body(MODEL, REQ)
    assert body["model"] == "gemini-omni-1.2-flash" and body["input"] == REQ.prompt
    assert body["response_format"] == {
        "type": "video", "aspect_ratio": "9:16", "resolution": "360p",
        "duration": "5s", "delivery": "uri"}


def test_body_puts_the_reference_image_before_the_prompt():
    body = gv.interaction_body(MODEL, gv.VideoRequest(
        prompt="she smiles", duration_s=3, aspect_ratio="16:9", resolution="720p",
        image=b"PNGDATA", image_mime="image/png"))
    image, text = body["input"]
    assert image == {"type": "image", "data": base64.b64encode(b"PNGDATA").decode(),
                     "mime_type": "image/png"}
    assert text == {"type": "text", "text": "she smiles"}


# ─── generation ──────────────────────────────────────────────────────────


async def test_uri_delivery_waits_for_the_file_then_downloads_it(monkeypatch):
    (video, meta), calls = await _generate(
        monkeypatch,
        (200, _interaction({"type": "video", "mime_type": "video/mp4", "uri": URI},
                           usage={"total_input_tokens": 21, "total_output_tokens": 9_655})),
        (200, {"name": "files/abc123", "state": "PROCESSING"}),
        (200, {"name": "files/abc123", "state": "ACTIVE"}),
        (200, MP4),
    )
    assert video == MP4
    assert [(c["method"], c["url"]) for c in calls] == [
        ("POST", "https://generativelanguage.googleapis.com/v1beta/interactions"),
        ("GET", "https://generativelanguage.googleapis.com/v1beta/files/abc123"),
        ("GET", "https://generativelanguage.googleapis.com/v1beta/files/abc123"),
        ("GET", URI)]
    assert all(c["key"] == "k" for c in calls)
    assert meta["mime_type"] == "video/mp4" and meta["video_s"] == 5.0
    assert (meta["tokens_in"], meta["tokens_out"]) == (21, 9_655)
    assert meta["cost_usd"] == pytest.approx((21 * 1.5 + 9_655 * 12) / 1e6)
    assert meta["model"] == MODEL


async def test_inline_clip_needs_no_file_polling_and_cost_falls_back_to_the_table(monkeypatch):
    (video, meta), calls = await _generate(monkeypatch, (200, {
        "status": "completed",
        "outputs": [{"type": "video", "data": base64.b64encode(MP4).decode()}]}))
    assert video == MP4 and len(calls) == 1
    assert meta["tokens_out"] == gv.video_output_tokens("360p", 5)
    assert meta["cost_usd"] == pytest.approx(gv.video_cost(MODEL, 0, 1931 * 5))


async def test_a_running_interaction_is_polled_by_id_until_it_completes(monkeypatch):
    done = _interaction({"type": "video", "data": base64.b64encode(MP4).decode()})
    (video, _), calls = await _generate(
        monkeypatch, (200, _interaction(None, status="in_progress")), (200, done))
    assert video == MP4
    assert calls[1]["url"].endswith("/interactions/v1_x") and calls[1]["method"] == "GET"


async def test_a_uri_without_a_file_id_is_downloaded_directly(monkeypatch):
    (video, _), calls = await _generate(
        monkeypatch,
        (200, _interaction({"type": "video", "uri": "https://cdn.example/clip.mp4"})),
        (200, MP4))
    assert video == MP4 and len(calls) == 2


# ─── failures ────────────────────────────────────────────────────────────


async def test_http_400_is_a_rejection_that_no_other_key_can_fix(monkeypatch):
    with pytest.raises(gv.VideoRejected, match="400"):
        await _generate(monkeypatch, (400, '{"error":{"status":"INVALID_ARGUMENT"}}'))
    assert issubclass(gv.VideoRejected, ValueError)   # the job queue never retries it


async def test_a_completed_answer_without_a_clip_is_a_rejection(monkeypatch):
    with pytest.raises(gv.VideoRejected, match="no clip"):
        await _generate(monkeypatch, (200, _interaction(None)))


@pytest.mark.parametrize("status, kind", [(429, "rate_limit"), (403, "auth"), (503, "error")])
async def test_provider_errors_keep_their_status_for_the_classifier(monkeypatch, status, kind):
    with pytest.raises(RuntimeError) as err:
        await _generate(monkeypatch, (status, "upstream said no"))
    assert not isinstance(err.value, gv.VideoRejected)
    assert classify_provider_error(err.value, "gemini") == kind


async def test_failed_interaction_failed_file_and_empty_clip_raise(monkeypatch):
    with pytest.raises(RuntimeError, match="ended failed"):
        await _generate(monkeypatch, (200, _interaction(None, status="failed")))
    with pytest.raises(RuntimeError, match="no id"):
        await _generate(monkeypatch, (200, {"status": "in_progress"}))
    with pytest.raises(RuntimeError, match="FAILED"):
        await _generate(monkeypatch,
                        (200, _interaction({"type": "video", "uri": URI})),
                        (200, {"state": "FAILED"}))
    with pytest.raises(RuntimeError, match="empty clip"):
        await _generate(monkeypatch,
                        (200, _interaction({"type": "video", "uri": URI})),
                        (200, {"state": "ACTIVE"}), (200, b""))
    with pytest.raises(RuntimeError, match="empty clip"):
        await _generate(monkeypatch, (200, _interaction({"type": "video"})))


async def test_network_failure_and_slow_upstream_do_not_look_like_a_timed_out_key(monkeypatch):
    """A slow render must not cool the paid key: it also serves speech and calls."""
    with pytest.raises(RuntimeError, match="unreachable") as err:
        await _generate(monkeypatch, httpx.ConnectError("refused"))
    assert classify_provider_error(err.value, "gemini") == "error"
    with pytest.raises(RuntimeError, match="gave up") as err:
        await _generate(monkeypatch, httpx.ReadTimeout("slow"))
    assert not isinstance(err.value, TimeoutError)
    assert classify_provider_error(err.value, "gemini") == "error"


async def test_the_whole_generation_shares_one_time_budget(monkeypatch):
    monkeypatch.setattr(gv, "get_settings", lambda: SimpleNamespace(
        GEMINI_VIDEO_TIMEOUT_S=0.0, GEMINI_VIDEO_POLL_S=0.0))
    send, calls = _script()
    monkeypatch.setattr(gv, "_send", send)
    with pytest.raises(RuntimeError, match="gave up on the create call"):
        await gv.generate_via_gemini(model=MODEL, request=REQ, api_key="k")
    assert calls == []


async def test_transport_class_delegates(monkeypatch):
    send, _ = _script((200, {"outputs": [
        {"type": "video", "data": base64.b64encode(MP4).decode()}]}))
    monkeypatch.setattr(gv, "_send", send)
    video, meta = await gv.GeminiVideoTransport().generate_video(
        model=MODEL, request=REQ, api_key="k")
    assert video == MP4 and meta["latency_ms"] >= 0
