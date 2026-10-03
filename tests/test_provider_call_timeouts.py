"""Hard wall-clock ceilings on embed / transcription (2026-10-03 review).

call_llm always had an asyncio.wait_for backstop; embed, atranscription, the
chat-transcription fallback and the gemini ASR call had none, so one hung
upstream pinned the request (and its cost reservation) until the client quit.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from aibroker.config import get_settings
from aibroker.providers import litellm_adapter as la
from aibroker.providers.provider_errors import classify_provider_error, is_timeout


async def _hang(*_a, **_kw):
    await asyncio.sleep(30)


async def test_embed_has_a_hard_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "EMBED_TIMEOUT_S", 0.05)
    with patch.object(la.litellm, "aembedding", _hang), pytest.raises(TimeoutError) as ei:
        await la.embed(model="voyage/voyage-4", texts=["x"], api_key="k")
    assert "embedding voyage/voyage-4" in str(ei.value)
    assert is_timeout(ei.value) and classify_provider_error(ei.value, "voyage") == "rate_limit"


async def test_whisper_transcription_has_a_hard_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "TRANSCRIBE_TIMEOUT_S", 0.05)
    with patch.object(la.litellm, "atranscription", _hang), pytest.raises(TimeoutError):
        await la.transcribe(model="groq/whisper-large-v3-turbo", audio=b"x",
                            filename="a.ogg", api_key="k")


async def test_chat_transcription_has_a_hard_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "TRANSCRIBE_TIMEOUT_S", 0.05)
    with patch.object(la.litellm, "acompletion", _hang), pytest.raises(TimeoutError):
        await la._transcribe_via_chat(model="gemini/gemini-2.5-flash", audio=b"x",
                                      filename="a.ogg", api_key="k")


async def test_gemini_asr_post_has_a_hard_timeout_despite_httpx_phase_timeouts(monkeypatch):
    monkeypatch.setattr(get_settings(), "GEMINI_ASR_TIMEOUT_S", 0.05)
    with patch.object(la, "_post_gemini_asr", _hang), pytest.raises(TimeoutError) as ei:
        await la._transcribe_via_gemini_asr(
            model="gemini/gemini-3.5-transcribe", audio=b"x", filename="a.ogg", api_key="k")
    assert "gemini-asr" in str(ei.value)


def test_timeout_settings_have_sane_defaults():
    s = get_settings()
    assert s.EMBED_TIMEOUT_S > 0 and s.TRANSCRIBE_TIMEOUT_S > 0 and s.GEMINI_ASR_TIMEOUT_S > 0
