"""Text-to-speech lane: services/speech_service.py and POST /v1/speech.

Paid-only key choice, honest cost reservation, rotation, caps, and the route
(scope, validation, 503, happy path). No real provider is called.
"""
from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import aibroker.services.speech_service as svc
from aibroker.main import app
from aibroker.routing.chains import CAPABILITY_CHAINS, scope_for
from aibroker.routing.cost_guard import CostGuardError
from tests.test_routes_proxy import _make_project

client = TestClient(app)
PROJECT = SimpleNamespace(id=1, name="sim")
MODEL = "gemini/gemini-3.8-flash-tts"


def _meta(cost=1.2e-3):
    return {"model": MODEL, "model_served": "gemini-3.8-flash-tts", "mime_type": "audio/wav",
            "tokens_in": 14, "tokens_out": 132, "cost_usd": cost, "audio_s": 5.3,
            "latency_ms": 900}


def _paid_key(kid=1):
    return SimpleNamespace(id=kid, label=f"paid{kid}", tier="paid",
                           provider="gemini", token_encrypted="x")


def _wire(monkeypatch, *, picks, synth, record=None, reserve=None):
    calls = {"pick": []}

    async def fake_pick(provider, scope, **kw):
        calls["pick"].append({"provider": provider, "scope": scope, **kw})
        i = len(calls["pick"]) - 1
        return picks[i] if i < len(picks) else None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "synthesize", synth)
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost",
                        reserve or AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.release_cost", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt._penalize", AsyncMock(return_value="error"))
    monkeypatch.setattr("aibroker.services.attempt._record_error", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.affinity.note_success",
                        AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.record_usage",
                        record or AsyncMock(return_value=77))
    return calls


async def _run(**kw):
    args = {"project": PROJECT, "text": "Привет", "voice": "Kore", "style": None,
            "model": None, "workflow": "voice"} | kw
    return await svc.run_speech(**args)


# ─── wiring ──────────────────────────────────────────────────────────────


def test_speech_is_its_own_paid_gemini_lane():
    assert CAPABILITY_CHAINS["speech"] == ["gemini"]
    assert scope_for("speech") == "llm:speech"


# ─── service ─────────────────────────────────────────────────────────────


async def test_only_paid_keys_with_the_speech_scope_are_asked_for(monkeypatch):
    calls = _wire(monkeypatch, picks=[_paid_key()],
                  synth=AsyncMock(return_value=(b"RIFF", _meta())))
    await _run()
    pick = calls["pick"][0]
    assert (pick["provider"], pick["scope"], pick["require_tier"]) == \
        ("gemini", "llm:speech", "paid")
    assert pick["models"] == [MODEL]


async def test_success_returns_audio_and_books_cost(monkeypatch):
    record = AsyncMock(return_value=77)
    synth = AsyncMock(return_value=(b"RIFFdata", _meta(cost=1.2e-3)))
    _wire(monkeypatch, picks=[_paid_key()], synth=synth, record=record)
    out = await _run(style="warmly")
    assert out.audio == b"RIFFdata" and out.mime_type == "audio/wav"
    assert out.request_id == 77 and out.key_label == "paid1" and out.audio_s == 5.3
    assert synth.await_args.kwargs | {"api_key": None} == {
        "model": MODEL, "text": "Привет", "voice": "Kore", "style": "warmly", "api_key": None}
    kw = record.await_args.kwargs
    assert (kw["capability"], kw["status"], kw["tokens_out"]) == ("speech", "ok", 132)
    assert kw["cost_usd"] == pytest.approx(1.2e-3)


async def test_reservation_is_not_zero(monkeypatch):
    reserve = AsyncMock(return_value=None)
    _wire(monkeypatch, picks=[_paid_key()],
          synth=AsyncMock(return_value=(b"RIFF", _meta())), reserve=reserve)
    await _run(text="x" * 500)
    assert reserve.await_args.kwargs["estimated_cost"] > 0


async def test_pinned_lite_model_is_used(monkeypatch):
    synth = AsyncMock(return_value=(b"RIFF", _meta()))
    calls = _wire(monkeypatch, picks=[_paid_key()], synth=synth)
    out = await _run(model="gemini-3.8-flash-lite-tts")
    assert out.model == "gemini/gemini-3.8-flash-lite-tts"
    assert calls["pick"][0]["models"] == ["gemini/gemini-3.8-flash-lite-tts"]


async def test_rotates_to_the_next_paid_key_on_failure(monkeypatch):
    n = {"i": 0}

    async def flaky(**kw):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("gemini-tts 503: overloaded")
        return b"RIFF", _meta()
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], synth=flaky)
    assert (await _run()).key_label == "paid2"


async def test_every_key_failing_raises_speech_failed(monkeypatch):
    async def dead(**kw):
        raise RuntimeError("boom")
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], synth=dead)
    with pytest.raises(svc.SpeechFailed, match="boom"):
        await _run()


async def test_no_paid_key_returns_none(monkeypatch):
    _wire(monkeypatch, picks=[], synth=AsyncMock())
    assert await _run() is None


async def test_spent_project_cap_stops_before_the_provider_is_called(monkeypatch):
    provider = AsyncMock()

    async def blocked(**kw):
        raise CostGuardError("project", 1.0, 1.0, 0.1)
    _wire(monkeypatch, picks=[_paid_key()], synth=provider, reserve=blocked)
    with pytest.raises(svc.SpeechFailed, match="budget"):
        await _run()
    provider.assert_not_awaited()


# ─── route ───────────────────────────────────────────────────────────────


def _outcome():
    return svc.SpeechOutcome(
        audio=b"RIFFdata", mime_type="audio/wav", provider="gemini", model=MODEL,
        voice="Aoede", audio_s=5.3, tokens_in=14, tokens_out=132, cost_usd=1.2e-3,
        latency_ms=900, key_label="paid1", request_id=77,
        model_served="gemini-3.8-flash-tts")


async def test_route_requires_the_speech_scope():
    plain, _ = await _make_project(["llm:chat", "llm:audio"])
    r = client.post("/v1/speech", headers={"X-Project-Key": plain}, json={"text": "hi"})
    assert r.status_code == 403


@pytest.mark.parametrize("body", [
    {"text": ""}, {"text": "   "}, {"text": "hi", "voice": "Alice"},
    {"text": "x" * 5001},
])
async def test_route_rejects_invalid_requests_with_422(body):
    plain, _ = await _make_project(["llm:speech"])
    with patch("aibroker.routes.proxy.run_speech", AsyncMock()) as run:
        r = client.post("/v1/speech", headers={"X-Project-Key": plain}, json=body)
    assert r.status_code == 422
    run.assert_not_awaited()


async def test_route_rejects_a_model_that_does_not_speak():
    plain, _ = await _make_project(["llm:speech"])
    with patch("aibroker.routes.proxy.run_speech", AsyncMock()) as run:
        r = client.post("/v1/speech", headers={"X-Project-Key": plain},
                        json={"text": "hi", "model": "gemini-2.5-flash"})
    assert r.status_code == 400
    run.assert_not_awaited()


async def test_route_503_without_a_paid_key():
    plain, _ = await _make_project(["llm:speech"])
    with patch("aibroker.routes.proxy.run_speech", AsyncMock(return_value=None)):
        r = client.post("/v1/speech", headers={"X-Project-Key": plain}, json={"text": "hi"})
    assert r.status_code == 503


async def test_route_502_when_every_key_fails():
    plain, _ = await _make_project(["llm:speech"])
    with patch("aibroker.routes.proxy.run_speech",
               AsyncMock(side_effect=svc.SpeechFailed("boom"))):
        r = client.post("/v1/speech", headers={"X-Project-Key": plain}, json={"text": "hi"})
    assert r.status_code == 502


async def test_route_happy_path():
    plain, _ = await _make_project(["llm:speech"])
    with patch("aibroker.routes.proxy.run_speech", AsyncMock(return_value=_outcome())) as run:
        r = client.post("/v1/speech", headers={"X-Project-Key": plain},
                        json={"text": "Привет", "voice": "Aoede", "style": "warmly",
                              "workflow": "voice"})
    assert r.status_code == 200
    data = r.json()
    assert base64.b64decode(data["audio_base64"]) == b"RIFFdata"
    assert data["mime_type"] == "audio/wav" and data["voice"] == "Aoede"
    assert data["request_id"] == 77 and data["tokens_out"] == 132
    kw = run.await_args.kwargs
    assert (kw["text"], kw["voice"], kw["style"], kw["workflow"]) == \
        ("Привет", "Aoede", "warmly", "voice")
