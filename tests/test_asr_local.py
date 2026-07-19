"""asr_local — /transcribe contract with the whisper model mocked out.

Separate deployable package (services/asr-local) — see pyproject.toml's
pythonpath for why this imports cleanly without pip-installing it.

TestClient is used WITHOUT the context manager on purpose: lifespan
(real model preload) must not run in unit tests.
"""
from __future__ import annotations

import base64

import asr_local.app as asr
import pytest
from fastapi.testclient import TestClient


class _FakeSegment:
    def __init__(self, text: str):
        self.text = text


class _FakeInfo:
    duration = 3.21
    language = "ru"


class _FakeModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, path, language=None, **kw):
        self.calls.append({"path": path, "language": language, **kw})
        return iter([_FakeSegment(" привет "), _FakeSegment("мир")]), _FakeInfo()


@pytest.fixture()
def client(monkeypatch):
    fake = _FakeModel()
    monkeypatch.setattr(asr, "get_model", lambda: fake)
    c = TestClient(asr.app)
    c.fake_model = fake
    return c


def test_healthz_reports_model_state(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["service"] == "asr-local"


def test_transcribe_raw_bytes(client):
    r = client.post("/transcribe", content=b"oggbytes",
                    headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "привет мир"
    assert body["duration_s"] == 3.2
    assert body["language"] == "ru"
    # DEFAULT_LANGUAGE is "auto" (multi-tenant service) — omitting the query
    # param must resolve to None (whisper's own auto-detect), not the literal
    # string "auto".
    assert client.fake_model.calls[0]["language"] is None


def test_transcribe_base64_json(client):
    b64 = base64.b64encode(b"oggbytes").decode("ascii")
    r = client.post("/transcribe", json={"b64": b64})
    assert r.status_code == 200
    assert r.json()["text"] == "привет мир"


def test_transcribe_language_override_and_auto(client):
    r = client.post("/transcribe", content=b"x", params={"language": "id"},
                    headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 200
    assert client.fake_model.calls[0]["language"] == "id"

    r = client.post("/transcribe", content=b"x", params={"language": "auto"},
                    headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 200
    assert client.fake_model.calls[1]["language"] is None


def test_first_pass_uses_vad(client):
    client.post("/transcribe", content=b"x", headers={"Content-Type": "audio/ogg"})
    assert client.fake_model.calls[0].get("vad_filter") is True


def test_empty_vad_result_retries_without_vad(monkeypatch):
    """VAD can clip a quiet/short REAL message to nothing. The service retries
    once WITHOUT the VAD gate before returning empty (which the broker would
    escalate to a paid cloud model) — recovers faint speech locally & free."""
    class _EmptyThenText:
        def __init__(self):
            self.calls = []

        def transcribe(self, path, language=None, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                return iter([]), _FakeInfo()                 # VAD pass → empty
            return iter([_FakeSegment("recovered speech")]), _FakeInfo()

    fake = _EmptyThenText()
    monkeypatch.setattr(asr, "get_model", lambda: fake)
    c = TestClient(asr.app)
    r = c.post("/transcribe", content=b"x", headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 200
    assert r.json()["text"] == "recovered speech"
    assert fake.calls[0]["vad_filter"] is True
    assert fake.calls[1]["vad_filter"] is False


def test_empty_both_passes_returns_empty(monkeypatch):
    """Genuinely silent audio: both passes empty → empty text (the broker then
    decides — it escalates a local empty to a cloud model)."""
    class _AlwaysEmpty:
        def __init__(self):
            self.calls = []

        def transcribe(self, path, language=None, **kw):
            self.calls.append(kw)
            return iter([]), _FakeInfo()

    fake = _AlwaysEmpty()
    monkeypatch.setattr(asr, "get_model", lambda: fake)
    c = TestClient(asr.app)
    r = c.post("/transcribe", content=b"x", headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 200
    assert r.json()["text"] == ""
    assert len(fake.calls) == 2   # tried with and without VAD


def test_transcribe_rejects_empty_body(client):
    r = client.post("/transcribe", content=b"")
    assert r.status_code == 400


def test_transcribe_rejects_bad_json(client):
    r = client.post("/transcribe", json={"nope": 1})
    assert r.status_code == 400


def test_transcribe_rejects_oversize(client):
    r = client.post("/transcribe", content=b"x" * (asr._MAX_AUDIO_BYTES + 1),
                    headers={"Content-Type": "audio/ogg"})
    assert r.status_code == 413
