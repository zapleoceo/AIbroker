"""Real-time voice lane: providers/gemini_live.py, services/live_service.py and
WebSocket /v1/live. Gemini is never contacted: the upstream is a fake socket or
a fake session, the caller is a fake LiveClient or the TestClient socket."""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import aibroker.providers.gemini_live as gl
import aibroker.services.live_service as svc
from aibroker.main import app
from aibroker.providers.catalog import input_modalities, output_modalities
from aibroker.providers.registry import model_for, model_spec
from aibroker.routing.chains import CAPABILITY_CHAINS, scope_for
from aibroker.routing.cost_guard import CostGuardError
from tests.test_routes_proxy import _make_project

client = TestClient(app)
PROJECT = SimpleNamespace(id=1, name="sim")
MODEL = "gemini/gemini-3.8-live"


# ─── wiring / pricing ───────────────────────────────────────────────────


def test_live_is_its_own_paid_gemini_lane():
    assert CAPABILITY_CHAINS["live"] == ["gemini"]
    assert scope_for("live") == "llm:live"
    assert model_for("gemini", "live") == MODEL
    spec = model_spec(MODEL)
    assert spec.transport == "gemini_live"
    assert input_modalities(spec) == ["audio"] and output_modalities(spec) == ["audio"]


def test_usage_splits_audio_and_text_by_modality():
    usage = gl.LiveUsage()
    usage.add({"promptTokenCount": 1000, "responseTokenCount": 300,
               "promptTokensDetails": [{"modality": "TEXT", "tokenCount": 800},
                                       {"modality": "AUDIO", "tokenCount": 200}],
               "responseTokensDetails": [{"modality": "AUDIO", "tokenCount": 300}]})
    usage.add({"promptTokenCount": 50, "responseTokenCount": 10})   # no details: all audio
    assert (usage.text_in, usage.audio_in, usage.text_out, usage.audio_out) == (800, 250, 0, 310)
    assert (usage.tokens_in, usage.tokens_out) == (1050, 310)


def test_cost_uses_audio_and_text_rates():
    usage = gl.LiveUsage(text_in=1_000_000, audio_in=1_000_000, text_out=1_000_000,
                         audio_out=1_000_000)
    assert gl.live_cost(MODEL, usage) == pytest.approx(0.75 + 3.0 + 4.5 + 12.0)
    assert gl.live_cost("gemini/unknown-live", usage) == 0.0


def test_reservation_covers_the_whole_session():
    one_minute = gl.estimate_live_cost(MODEL, 60)
    assert one_minute == pytest.approx((60 * 32 * 3.0 + 60 * 25 * 12.0) / 1e6)
    assert gl.estimate_live_cost(MODEL, 540) > one_minute


# ─── protocol translation ───────────────────────────────────────────────


def test_setup_message_carries_voice_prompt_and_transcription():
    msg = gl.setup_message(model=MODEL, voice="Kore", system="Ты Марина", language="ru-RU")
    setup = msg["setup"]
    assert setup["model"] == "models/gemini-3.8-live"
    config = setup["generationConfig"]
    assert config["responseModalities"] == ["AUDIO"]
    assert config["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert config["speechConfig"]["languageCode"] == "ru-RU"
    assert setup["systemInstruction"] == {"parts": [{"text": "Ты Марина"}]}
    assert setup["inputAudioTranscription"] == {} == setup["outputAudioTranscription"]
    bare = gl.setup_message(model=MODEL, voice="Kore", system=" ", language=None)["setup"]
    assert "systemInstruction" not in bare
    assert "languageCode" not in bare["generationConfig"]["speechConfig"]


def test_audio_and_text_messages():
    audio = gl.audio_message(b"\x01\x02")["realtimeInput"]["audio"]
    assert base64.b64decode(audio["data"]) == b"\x01\x02"
    assert audio["mimeType"] == "audio/pcm;rate=16000"
    turn = gl.text_message("Алло")["clientContent"]
    assert turn["turns"][0]["parts"][0]["text"] == "Алло" and turn["turnComplete"] is True


def test_server_messages_become_events():
    pcm = base64.b64encode(b"PCM").decode()
    parsed = gl.parse_server_message({
        "serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": pcm}}, {"text": "x"}]},
                          "inputTranscription": {"text": "привет"},
                          "outputTranscription": {"text": "здравствуйте"},
                          "interrupted": True, "turnComplete": True},
        "usageMetadata": {"promptTokenCount": 5},
        "goAway": {"timeLeft": "10s"},
    })
    assert [e.kind for e in parsed.events] == [
        "audio", "input_text", "output_text", "interrupted", "turn_complete", "go_away"]
    assert parsed.events[0].audio == b"PCM" and parsed.events[1].text == "привет"
    assert parsed.usage == {"promptTokenCount": 5}
    assert gl.parse_server_message({"setupComplete": {}}).events == []


class FakeSocket:
    def __init__(self, incoming: list) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def recv(self):
        if not self.incoming:
            raise gl.LiveClosed
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, bytes | str) else json.dumps(item)

    async def close(self) -> None:
        self.closed = True
        raise RuntimeError("already closed")   # close() must swallow it


class ConnectionClosedOK(Exception):
    """Named like websockets' own class: the session treats it as a normal end."""


async def test_session_setup_events_usage_and_close():
    pcm = base64.b64encode(b"AB").decode()
    ws = FakeSocket([{"setupComplete": {}},
                     {"serverContent": {"modelTurn": {"parts": [{"inlineData": {"data": pcm}}]}},
                      "usageMetadata": {"promptTokenCount": 10, "responseTokenCount": 4}},
                     b'{"serverContent": {"turnComplete": true}}',
                     ConnectionClosedOK()])
    session = gl.GeminiLiveSession(ws, MODEL)
    await session.setup(voice="Kore", system="s", language=None)
    await session.send_audio(b"\x00\x00")
    await session.send_text("hi")
    events = [e async for e in session.events()]
    assert [e.kind for e in events] == ["audio", "turn_complete"]
    assert (session.usage.tokens_in, session.usage.tokens_out) == (10, 4)
    assert "setup" in ws.sent[0] and "realtimeInput" in ws.sent[1] and "clientContent" in ws.sent[2]
    await session.close()
    assert ws.closed


async def test_session_setup_refused_and_unexpected_errors_raise():
    with pytest.raises(RuntimeError, match="setup refused"):
        await gl.GeminiLiveSession(FakeSocket([{"error": "bad"}]), MODEL).setup(
            voice="Kore", system="", language=None)
    session = gl.GeminiLiveSession(FakeSocket([ValueError("boom")]), MODEL)
    with pytest.raises(ValueError):
        _ = [e async for e in session.events()]
    assert [e async for e in gl.GeminiLiveSession(FakeSocket([]), MODEL).events()] == []


async def test_transport_opens_through_connect(monkeypatch):
    connect = AsyncMock(return_value="session")
    monkeypatch.setattr(gl, "connect_gemini_live", connect)
    out = await gl.GeminiLiveTransport().open_live(
        model=MODEL, voice="Kore", system="s", language=None, api_key="k", timeout=5)
    assert out == "session" and connect.await_args.kwargs["api_key"] == "k"


# ─── relay ──────────────────────────────────────────────────────────────


class FakeSession:
    def __init__(self, events=(), *, hang=False, fail=False) -> None:
        self._events = list(events)
        self.hang = hang
        self.fail = fail
        self.audio: list[bytes] = []
        self.texts: list[str] = []
        self.closed = False
        self.usage = gl.LiveUsage(text_in=100, audio_in=320, audio_out=250)
        self.model_served = "gemini-3.8-live"

    async def events(self):
        for event in self._events:
            yield event
        if self.fail:
            raise RuntimeError("upstream broke")
        if self.hang:
            await asyncio.Event().wait()

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def send_text(self, text: str) -> None:
        self.texts.append(text)

    async def close(self) -> None:
        self.closed = True


class FakeCaller:
    def __init__(self, messages=(), *, hang=False) -> None:
        self.messages = list(messages)
        self.hang = hang
        self.audio: list[bytes] = []
        self.events: list[dict] = []

    async def receive(self):
        await asyncio.sleep(0)
        if self.messages:
            return self.messages.pop(0)
        if self.hang:
            await asyncio.Event().wait()
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.audio.append(pcm)

    async def send_event(self, event: dict) -> None:
        self.events.append(event)


E = gl.LiveEvent


async def test_relay_carries_audio_both_ways_and_keeps_the_transcript():
    session = FakeSession([E("input_text", text="Алло, "), E("input_text", text="это кто?"),
                           E("output_text", text="Марина."), E("audio", audio=b"OUT"),
                           E("interrupted"), E("turn_complete")], hang=True)
    caller = FakeCaller([b"IN1", b"", {"type": "text", "text": "печатаю"}, {"type": "noise"}],
                        hang=True)
    caller.messages.append({"type": "end"})
    transcript, reason = await svc.relay(session, caller, max_seconds=5)
    assert reason == "caller_ended" and session.closed
    assert session.audio == [b"IN1"] and session.texts == ["печатаю"]
    assert caller.audio == [b"OUT"]
    assert transcript.cleaned() == [{"role": "user", "text": "Алло, это кто?"},
                                    {"role": "assistant", "text": "Марина."}]
    assert {"type": "interrupted"} in caller.events and {"type": "turn_complete"} in caller.events


async def test_relay_end_reasons():
    hung_up = await svc.relay(FakeSession(hang=True), FakeCaller(), max_seconds=5)
    assert hung_up[1] == "caller_hung_up"
    go_away = await svc.relay(FakeSession([E("go_away")]), FakeCaller(hang=True), max_seconds=5)
    assert go_away[1] == "upstream_go_away"
    closed = await svc.relay(FakeSession(), FakeCaller(hang=True), max_seconds=5)
    assert closed[1] == "upstream_closed"
    broke = await svc.relay(FakeSession(fail=True), FakeCaller(hang=True), max_seconds=5)
    assert broke[1] == "upstream_error"
    timed = await svc.relay(FakeSession(hang=True), FakeCaller(hang=True), max_seconds=0.05)
    assert timed[1] == "time_limit"


# ─── run_live (key walk, money) ─────────────────────────────────────────


def _paid_key(kid=1):
    return SimpleNamespace(id=kid, label=f"paid{kid}", tier="paid",
                           provider="gemini", token_encrypted="x")


def _wire(monkeypatch, *, picks, opener, record=None, reserve=None):
    calls = {"pick": []}

    async def fake_pick(provider, scope, **kw):
        calls["pick"].append({"provider": provider, "scope": scope, **kw})
        i = len(calls["pick"]) - 1
        return picks[i] if i < len(picks) else None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "open_live", opener)
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost",
                        reserve or AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.release_cost", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt._penalize", AsyncMock(return_value="error"))
    monkeypatch.setattr("aibroker.services.attempt._record_error", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.record_usage",
                        record or AsyncMock(return_value=91))
    return calls


def _req(**kw):
    return svc.LiveRequest(**({"voice": "Kore", "system": "Ты Марина", "workflow": "call"} | kw))


async def test_call_books_the_real_cost_and_returns_the_transcript(monkeypatch):
    session = FakeSession([E("input_text", text="Привет"), E("output_text", text="Привет!")],
                          hang=True)
    record = AsyncMock(return_value=91)
    reserve = AsyncMock(return_value=None)
    opener = AsyncMock(return_value=session)
    calls = _wire(monkeypatch, picks=[_paid_key()], opener=opener, record=record,
                  reserve=reserve)
    caller = FakeCaller([b"IN"])
    out = await svc.run_live(project=PROJECT, req=_req(greeting="Позвонили, поздоровайся",
                                                      max_seconds=99999), client=caller)
    pick = calls["pick"][0]
    assert (pick["scope"], pick["require_tier"], pick["models"]) == ("llm:live", "paid", [MODEL])
    assert opener.await_args.kwargs | {"api_key": None, "timeout": None} == {
        "model": MODEL, "voice": "Kore", "system": "Ты Марина", "language": None,
        "api_key": None, "timeout": None}
    assert caller.events[0]["type"] == "ready" and caller.events[0]["max_seconds"] == 540
    assert session.texts == ["Позвонили, поздоровайся"]
    assert out.transcript == [{"role": "user", "text": "Привет"},
                              {"role": "assistant", "text": "Привет!"}]
    assert (out.request_id, out.key_label, out.end_reason) == (91, "paid1", "caller_hung_up")
    assert (out.tokens_in, out.tokens_out) == (420, 250)
    kw = record.await_args.kwargs
    assert (kw["capability"], kw["status"], kw["workflow"]) == ("live", "ok", "call")
    assert kw["cost_usd"] == pytest.approx(gl.live_cost(MODEL, session.usage))
    assert reserve.await_args.kwargs["estimated_cost"] == pytest.approx(
        gl.estimate_live_cost(MODEL, 540))


async def test_a_key_that_cannot_connect_is_skipped(monkeypatch):
    opener = AsyncMock(side_effect=[RuntimeError("gemini-live 503: overloaded"),
                                    FakeSession(hang=True)])
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)], opener=opener)
    out = await svc.run_live(project=PROJECT, req=_req(), client=FakeCaller())
    assert out.key_label == "paid2"


async def test_every_key_failing_raises_live_failed(monkeypatch):
    _wire(monkeypatch, picks=[_paid_key(1), _paid_key(2)],
          opener=AsyncMock(side_effect=RuntimeError("boom")))
    with pytest.raises(svc.LiveFailed, match="boom"):
        await svc.run_live(project=PROJECT, req=_req(), client=FakeCaller())


async def test_no_paid_key_returns_none(monkeypatch):
    _wire(monkeypatch, picks=[], opener=AsyncMock())
    assert await svc.run_live(project=PROJECT, req=_req(), client=FakeCaller()) is None


async def test_spent_cap_stops_before_connecting(monkeypatch):
    opener = AsyncMock()

    async def blocked(**kw):
        raise CostGuardError("project", 1.0, 1.0, 0.1)
    _wire(monkeypatch, picks=[_paid_key()], opener=opener, reserve=blocked)
    with pytest.raises(svc.LiveFailed, match="budget"):
        await svc.run_live(project=PROJECT, req=_req(), client=FakeCaller())
    opener.assert_not_awaited()


# ─── route ──────────────────────────────────────────────────────────────


def _closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        ws.receive_text()
    return e.value.code


async def test_route_refuses_without_a_valid_key():
    with client.websocket_connect("/v1/live") as ws:
        assert ws.receive_json()["code"] == 401
        assert _closed_with(ws) == 4401
    with client.websocket_connect("/v1/live", headers={"X-Project-Key": "nope"}) as ws:
        assert ws.receive_json()["detail"] == "Invalid project key"


async def test_route_requires_the_live_scope():
    plain, _ = await _make_project(["llm:speech"])
    with client.websocket_connect("/v1/live", headers={"X-Project-Key": plain}) as ws:
        assert ws.receive_json()["code"] == 403
        assert _closed_with(ws) == 4403


@pytest.mark.parametrize("start, code", [
    ({"type": "start", "voice": "Alice"}, 422),
    ({"type": "hello"}, 422),
    ({"type": "start", "model": "gemini-2.5-flash"}, 400),
])
async def test_route_rejects_bad_start_messages(start, code):
    plain, _ = await _make_project(["llm:live"])
    with patch("aibroker.routes.live.run_live", AsyncMock()) as run, \
            client.websocket_connect("/v1/live", headers={"X-Project-Key": plain}) as ws:
        ws.send_json(start)
        assert ws.receive_json()["code"] == code
    run.assert_not_awaited()


async def test_route_rejects_audio_before_start():
    plain, _ = await _make_project(["llm:live"])
    with client.websocket_connect("/v1/live", headers={"X-Project-Key": plain}) as ws:
        ws.send_bytes(b"\x00\x00")
        assert ws.receive_json()["code"] == 422


@pytest.mark.parametrize("effect, code", [
    ({"return_value": None}, 503), ({"side_effect": svc.LiveFailed("boom")}, 502),
])
async def test_route_maps_service_failures(effect, code):
    plain, _ = await _make_project(["llm:live"])
    with patch("aibroker.routes.live.run_live", AsyncMock(**effect)), \
            client.websocket_connect("/v1/live", headers={"X-Project-Key": plain}) as ws:
        ws.send_json({"type": "start"})
        assert ws.receive_json()["code"] == code
        assert _closed_with(ws) == 4000 + code


async def test_route_happy_path_streams_and_reports():
    plain, _ = await _make_project(["llm:live"])
    seen = {}

    async def fake_run(*, project, client, req):
        seen["req"] = req
        await client.send_event({"type": "ready"})
        seen["audio"] = await client.receive()
        await client.send_audio(b"OUT")
        seen["end"] = await client.receive()
        return svc.LiveOutcome(transcript=[{"role": "user", "text": "Алло"}], provider="gemini",
                               model=MODEL, voice="Kore", duration_s=3.2, tokens_in=10,
                               tokens_out=5, cost_usd=0.001, key_label="paid1",
                               request_id=91, end_reason="caller_ended")

    with patch("aibroker.routes.live.run_live", fake_run), \
            client.websocket_connect("/v1/live", headers={"X-Project-Key": plain}) as ws:
        ws.send_json({"type": "start", "voice": "Kore", "system": "Ты Марина",
                      "language": "ru-RU", "greeting": "поздоровайся", "workflow": "call"})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"IN")
        assert ws.receive_bytes() == b"OUT"
        ws.send_text("not json")
        done = ws.receive_json()
        assert _closed_with(ws) == 1000
    assert seen["audio"] == b"IN" and seen["end"] == {}
    assert (seen["req"].system, seen["req"].language, seen["req"].greeting) == \
        ("Ты Марина", "ru-RU", "поздоровайся")
    assert done["type"] == "done" and done["request_id"] == 91
    assert done["transcript"] == [{"role": "user", "text": "Алло"}]
    assert done["cost_usd"] == 0.001 and done["end_reason"] == "caller_ended"
