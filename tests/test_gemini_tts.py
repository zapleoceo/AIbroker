"""Gemini TTS transport (providers/gemini_tts.py). No real provider is called."""
from __future__ import annotations

import base64
from unittest.mock import patch

import httpx
import pytest

import aibroker.providers.gemini_tts as tts
from aibroker.providers import transport as t
from aibroker.providers.provider_errors import classify_provider_error
from aibroker.providers.registry import model_for, model_spec

MODEL = "gemini/gemini-3.8-flash-tts"
WAV = b"RIFF" + b"\x00" * 64


def _ok_body(audio: bytes = WAV, tokens_in: int = 14, tokens_out: int = 132) -> dict:
    return {
        "candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/wav", "data": base64.b64encode(audio).decode()}}]},
            "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": tokens_in, "candidatesTokenCount": tokens_out},
        "modelVersion": "gemini-3.8-flash-tts",
    }


def _post_returning(status: int, body: dict | str, seen: dict | None = None):
    async def fake_post(url, api_key, body_json, timeout):
        if seen is not None:
            seen.update(url=url, key=api_key, body=body_json)
        req = httpx.Request("POST", url)
        if isinstance(body, dict):
            return httpx.Response(status, json=body, request=req)
        return httpx.Response(status, text=body, request=req)
    return fake_post


def test_speech_lane_defaults_to_the_newest_gemini_tts_model():
    assert model_for("gemini", "speech") == MODEL
    spec = model_spec(MODEL)
    assert spec.transport == "gemini_tts" and spec.pricing == "override"
    lite = model_spec("gemini/gemini-3.8-flash-lite-tts")
    assert "speech" in lite.capabilities and lite.output_usd_per_mtok < spec.output_usd_per_mtok


def test_transport_is_registered_and_implements_the_protocol():
    assert isinstance(t._load()["gemini_tts"], t.SpeechTransport)


async def test_synthesize_sends_voice_and_style_and_returns_audio():
    seen: dict = {}
    with patch.object(tts, "_post_gemini_tts", _post_returning(200, _ok_body(), seen)):
        audio, meta = await tts.synthesize_via_gemini(
            model=MODEL, text="Привет!", voice="Kore", style="warmly", api_key="k")
    assert audio == WAV
    assert seen["url"].endswith("/models/gemini-3.8-flash-tts:generateContent")
    assert seen["key"] == "k"
    cfg = seen["body"]["generationConfig"]
    assert cfg["responseModalities"] == ["AUDIO"]
    assert cfg["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "warmly: Привет!"
    assert meta["mime_type"] == "audio/wav"
    assert (meta["tokens_in"], meta["tokens_out"]) == (14, 132)
    assert meta["audio_s"] == pytest.approx(5.3)
    # $0.50 / 1M text in + $9.00 / 1M audio out
    assert meta["cost_usd"] == pytest.approx((14 * 0.5 + 132 * 9.0) / 1e6)


async def test_no_style_sends_the_text_verbatim():
    seen: dict = {}
    with patch.object(tts, "_post_gemini_tts", _post_returning(200, _ok_body(), seen)):
        await tts.synthesize_via_gemini(model=MODEL, text="Hi", voice="Puck",
                                        style="  ", api_key="k")
    assert seen["body"]["contents"][0]["parts"][0]["text"] == "Hi"


async def test_http_error_keeps_status_for_the_classifier():
    with patch.object(tts, "_post_gemini_tts", _post_returning(429, "RESOURCE_EXHAUSTED")), \
            pytest.raises(RuntimeError) as ei:
        await tts.synthesize_via_gemini(model=MODEL, text="x", voice="Kore",
                                        style=None, api_key="k")
    assert "429" in str(ei.value)
    assert classify_provider_error(ei.value, "gemini") == "rate_limit"


async def test_reply_without_audio_is_a_failure_not_an_empty_success():
    body = {"candidates": [{"content": {"parts": [{"text": "sorry"}]}}]}
    with patch.object(tts, "_post_gemini_tts", _post_returning(200, body)), \
            pytest.raises(RuntimeError, match="no audio"):
        await tts.synthesize_via_gemini(model=MODEL, text="x", voice="Kore",
                                        style=None, api_key="k")


async def test_network_error_is_wrapped():
    async def boom(*a, **kw):
        raise httpx.ConnectError("down")
    with patch.object(tts, "_post_gemini_tts", boom), pytest.raises(RuntimeError,
                                                                     match="unreachable"):
        await tts.synthesize_via_gemini(model=MODEL, text="x", voice="Kore",
                                        style=None, api_key="k")


def test_reservation_is_positive_and_grows_with_text():
    short = tts.estimate_speech_cost(MODEL, "x" * 10)
    long = tts.estimate_speech_cost(MODEL, "x" * 1000)
    assert 0 < short < long


def test_unpriced_model_costs_zero():
    assert tts.tts_cost("gemini/not-a-model", 10, 10) == 0.0
