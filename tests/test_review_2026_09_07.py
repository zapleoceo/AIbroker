"""Regressions from the 2026-09-07 review that don't belong to one existing file:
credential scrubbing in last_error, per-minute whisper pricing, and the cost
guard finally being consulted on the embed/transcribe paths."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.providers.litellm_adapter import (
    estimate_transcription_cost,
    whisper_cost,
)
from aibroker.routing.cost_guard import CostGuardError
from aibroker.services import llm_service
from aibroker.services.llm_service import (
    EmbedFailed,
    TranscribeFailed,
    _scrub_secrets,
    run_embed,
    run_transcribe,
)

# ─── last_error scrubbing ───────────────────────────────────────────────────


@pytest.mark.parametrize("raw", [
    "AuthenticationError: invalid key sk-proj-AbCdEfGhIjKlMnOpQrStUvWx",
    "403 for key AIzaSyD-0123456789abcdefghijklmnop",
    "GroqException: gsk_0123456789abcdef0123456789abcdef rejected",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature-part",
    "GET /v1?api_key=abcdefghijklmnop&x=1 failed",
])
def test_scrub_removes_key_shaped_substrings(raw):
    out = _scrub_secrets(raw)
    assert "[redacted]" in out
    for needle in ("sk-proj", "AIzaSy", "gsk_0123", "eyJhbGci", "abcdefghijklmnop"):
        assert needle not in out


def test_scrub_leaves_ordinary_errors_alone():
    msg = "RateLimitError: quota exceeded for model gemini-2.5-flash, retry in 20s"
    assert _scrub_secrets(msg) == msg


# ─── whisper pricing ────────────────────────────────────────────────────────


def test_whisper_cost_is_per_minute():
    assert whisper_cost("openai/whisper-1", 60.0) == pytest.approx(0.006)
    assert whisper_cost("openai/whisper-1", 150.0) == pytest.approx(0.015)


def test_whisper_cost_unpriced_model_is_zero():
    assert whisper_cost("groq/some-future-asr", 60.0) == 0.0


def test_estimate_transcription_cost_scales_with_bytes_and_is_free_for_local():
    small = estimate_transcription_cost("openai/whisper-1", 100_000)
    big = estimate_transcription_cost("openai/whisper-1", 1_000_000)
    assert 0 < small < big
    assert estimate_transcription_cost("local/whisper", 5_000_000) == 0.0


# ─── caps on embed / transcribe ─────────────────────────────────────────────


def _key(tier="paid", kid=7):
    return SimpleNamespace(id=kid, provider="voyage", label="k", tier=tier,
                           token_encrypted="enc", account_id=None)


def _project():
    return SimpleNamespace(id=1, name="vera")


async def test_run_embed_reserves_and_releases_around_the_call():
    """REGRESSION: reserve_cost had exactly one caller (the chat path), so
    /v1/embed never consulted the project/global caps at all."""
    reserve = AsyncMock()
    release = AsyncMock()
    with patch.object(llm_service, "pick_and_reserve", AsyncMock(return_value=_key())), \
         patch.object(llm_service, "estimate_llm_cost", lambda *a, **k: 0.01), \
         patch.object(llm_service, "reserve_cost", reserve), \
         patch.object(llm_service, "release_cost", release), \
         patch.object(llm_service, "decrypt", lambda _: "plain"), \
         patch.object(llm_service, "embed", AsyncMock(return_value=(
             [[0.1, 0.2]], {"tokens_in": 5, "cost_usd": 0.0001, "latency_ms": 3}))), \
         patch.object(llm_service, "record_usage", AsyncMock(return_value=42)), \
         patch.object(llm_service, "note_affinity_shared", AsyncMock()):
        out = await run_embed(project=_project(), provider="voyage",
                              inputs=["hello world"], model=None, workflow=None)
    assert out is not None and out.request_id == 42
    reserve.assert_awaited_once()
    release.assert_awaited_once()


async def test_run_embed_project_cap_stops_the_walk_with_a_clear_error():
    blocked = CostGuardError(kind="project", limit=0.2, used=0.2, attempted=0.01)
    with patch.object(llm_service, "pick_and_reserve", AsyncMock(return_value=_key())), \
         patch.object(llm_service, "reserve_cost", AsyncMock(side_effect=blocked)), \
         patch.object(llm_service, "record_usage", AsyncMock(return_value=1)), \
         patch.object(llm_service, "audit", AsyncMock()), \
         patch.object(llm_service, "embed", AsyncMock()) as call, \
         pytest.raises(EmbedFailed, match="budget cap"):
        await run_embed(project=_project(), provider="voyage",
                        inputs=["x"], model=None, workflow=None)
    call.assert_not_awaited()          # never reached the provider


async def test_run_embed_per_key_cap_tries_the_next_key():
    per_key = CostGuardError(kind="key", limit=1.0, used=1.0, attempted=0.01)
    keys = [_key(kid=1), _key(kid=2)]
    reserve = AsyncMock(side_effect=[per_key, None])
    with patch.object(llm_service, "pick_and_reserve", AsyncMock(side_effect=keys)), \
         patch.object(llm_service, "reserve_cost", reserve), \
         patch.object(llm_service, "release_cost", AsyncMock()), \
         patch.object(llm_service, "record_usage", AsyncMock(return_value=9)), \
         patch.object(llm_service, "audit", AsyncMock()), \
         patch.object(llm_service, "decrypt", lambda _: "plain"), \
         patch.object(llm_service, "embed", AsyncMock(return_value=(
             [[0.0]], {"tokens_in": 1, "cost_usd": 0.0, "latency_ms": 1}))), \
         patch.object(llm_service, "note_affinity_shared", AsyncMock()):
        out = await run_embed(project=_project(), provider="voyage",
                              inputs=["x"], model=None, workflow=None)
    assert out is not None
    assert reserve.await_count == 2


async def test_run_embed_releases_reservation_when_provider_fails():
    release = AsyncMock()
    # A positive estimate is the precondition: a $0 estimate reserves nothing
    # and (correctly) releases nothing. Pin it rather than depend on the
    # pricing table for a 1-char input.
    with patch.object(llm_service, "pick_and_reserve", AsyncMock(side_effect=[_key(), None])), \
         patch.object(llm_service, "estimate_llm_cost", lambda *a, **k: 0.01), \
         patch.object(llm_service, "reserve_cost", AsyncMock()), \
         patch.object(llm_service, "release_cost", release), \
         patch.object(llm_service, "decrypt", lambda _: "plain"), \
         patch.object(llm_service, "embed", AsyncMock(side_effect=RuntimeError("boom"))), \
         patch.object(llm_service, "_handle_attempt_failure", AsyncMock()), \
         pytest.raises(EmbedFailed):
        await run_embed(project=_project(), provider="voyage",
                        inputs=["x"], model=None, workflow=None)
    release.assert_awaited_once()


async def test_run_transcribe_project_cap_is_enforced():
    """REGRESSION: /v1/transcribe never reserved either — and paid whisper was
    booked at $0.00, so even a per-key cap was decorative."""
    blocked = CostGuardError(kind="global", limit=20.0, used=20.0, attempted=0.01)
    key = SimpleNamespace(id=3, provider="openai", label="w", tier="paid",
                          token_encrypted="enc", account_id=None)
    with patch.object(llm_service, "chain_for", lambda _: ["openai"]), \
         patch.object(llm_service, "pick_and_reserve", AsyncMock(return_value=key)), \
         patch.object(llm_service, "model_for", lambda p, c: "openai/whisper-1"), \
         patch.object(llm_service, "reserve_cost", AsyncMock(side_effect=blocked)), \
         patch.object(llm_service, "record_usage", AsyncMock(return_value=1)), \
         patch.object(llm_service, "audit", AsyncMock()), \
         patch.object(llm_service, "transcribe", AsyncMock()) as call, \
         pytest.raises(TranscribeFailed, match="budget cap"):
        await run_transcribe(project=_project(), audio=b"\x00" * 4000,
                             filename="v.ogg", workflow=None)
    call.assert_not_awaited()
