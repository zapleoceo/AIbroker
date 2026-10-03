"""reserve_cost → release_cost must pair on EVERY exit (2026-10-03 review).

Leaks fixed: decrypt() ran after reserve_cost but outside the try (chat, embed,
transcribe), and `except Exception` missed asyncio.CancelledError (a client
disconnect) — either left the estimate on the key's daily cost counter until
midnight, silently shrinking the cap.
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

import aibroker.services.attempt as att
import aibroker.services.llm_service as svc

PROJECT = SimpleNamespace(id=1, name="p")


def _paid_key():
    return SimpleNamespace(id=1, label="k", tier="paid", provider="mistral",
                           token_encrypted="x", daily_cost_cap_usd=5.0, account_id=None)


class _Ledger:
    def __init__(self):
        self.reserved = 0
        self.released = 0


@pytest.fixture
def ledger(monkeypatch):
    led = _Ledger()
    key = _paid_key()

    async def fake_pick(provider, scope, **kw):
        # one key per walk: hand it out once then None
        if led.reserved:
            return None
        return key

    async def fake_reserve(**kw):
        led.reserved += 1

    async def fake_release(**kw):
        led.released += 1

    async def noop(**kw):
        return 1

    async def fake_penalize(k, e, **kw):
        return "error"

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost", fake_reserve)
    monkeypatch.setattr("aibroker.services.attempt.release_cost", fake_release)
    monkeypatch.setattr("aibroker.services.attempt.record_usage", noop)
    monkeypatch.setattr("aibroker.services.attempt._penalize", fake_penalize)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["mistral"])
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "estimate_llm_cost", lambda *a, **k: 0.5)
    monkeypatch.setattr(svc, "estimate_transcription_cost", lambda *a, **k: 0.5)
    return led


def _boom_decrypt(_):
    raise ValueError("token cannot be decrypted")


async def _cancelled(**kw):
    raise asyncio.CancelledError


async def _chat():
    return await svc.run_chat(
        project=PROJECT, capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None, max_tokens=64,
        temperature=0.1, response_format=None, workflow="w")


async def _embed():
    return await svc.run_embed(project=PROJECT, provider="mistral", inputs=["x"],
                               model=None, workflow="w")


async def _transcribe():
    return await svc.run_transcribe(project=PROJECT, audio=b"x", filename="a.ogg", workflow="w")


@pytest.mark.parametrize("runner", [_chat, _embed, _transcribe])
async def test_decrypt_failure_after_reserve_releases_the_reservation(monkeypatch, ledger, runner):
    monkeypatch.setattr("aibroker.services.attempt.decrypt", _boom_decrypt)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["mistral"])
    with contextlib.suppress(svc.EmbedFailed, svc.TranscribeFailed):
        await runner()  # the walk giving up is fine — the point is the ledger
    assert ledger.reserved == 1
    assert ledger.released == 1


@pytest.mark.parametrize("runner,target", [
    (_chat, "call_llm"), (_embed, "embed"), (_transcribe, "transcribe"),
])
async def test_cancellation_releases_the_reservation_and_propagates(
        monkeypatch, ledger, runner, target):
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, target, _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await runner()
    assert ledger.reserved == 1
    assert ledger.released == 1


async def test_decision_cancellation_releases_the_reservation(monkeypatch, ledger):
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "decide", _cancelled)
    monkeypatch.setattr(svc, "estimate_tokens", lambda *a, **k: 10)
    monkeypatch.setattr(svc, "scope_for", lambda c: "llm:decision")
    with pytest.raises(asyncio.CancelledError):
        await svc.run_decision(project=PROJECT, state="s", questions={"q": {}},
                               model=None, workflow="w")
    assert ledger.reserved == 1 and ledger.released == 1


async def test_release_failure_never_masks_the_attempt_outcome(monkeypatch):
    async def bad_release(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("aibroker.services.attempt.release_cost", bad_release)
    await att._release_reservation(_paid_key(), 0.5)  # must not raise
