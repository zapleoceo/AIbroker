"""routes/proxy — /v1/chat and /v1/embed happy paths + fallback + errors.

All tests mock `pick_and_reserve` + `call_llm` + `embed` + `record_usage`
so they don't need real Postgres / real LLM providers.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from aibroker.auth import generate_project_key, hash_project_key
from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, DeepJobRow, ProjectRow
from aibroker.main import app
from aibroker.services.deep_jobs import next_poll_after_s
from aibroker.services.llm_service import run_chat

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")

client = TestClient(app)


# ─── Fixture: in-DB project + key (uses default test fixture's SQLite) ───


_PID_COUNTER = [1000]


async def _make_project(scopes: list[str]) -> tuple[str, int]:
    """Insert a project, return (project_key_plain, project_id).

    Provides explicit id since SQLite BIGINT doesn't autoincrement.
    """
    plain = generate_project_key()
    _PID_COUNTER[0] += 1
    pid = _PID_COUNTER[0]
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid,
            name=f"proxy_test_{pid}",
            project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12],
            allowed_scopes=scopes,
            is_active=True,
            notes="",
        ))
    return plain, pid


def _fake_key():
    """A fake ApiKeyRow object (not from DB) suitable for proxy logic."""
    return ApiKeyRow(
        id=1, provider="cerebras", label="t",
        token_encrypted=encrypt("fake-token"),
        tier="free", scopes=["llm:chat"],
        is_active=True, is_alive=True,
        error_count=0,
    )


# ─── sync /v1/chat removed — chat is async-only via /v1/jobs (2026-07-10) ────
#
# The broker is async-only for chat. run_chat's orchestration is unchanged
# (the job dispatcher runs it) — these tests exercise it DIRECTLY (the mocks
# already patch it at the service level), rather than through the removed HTTP
# endpoint. Capability/scope validation lives on /v1/jobs now (see the jobs
# tests below). The one HTTP assertion left is that /v1/chat returns 410.

_PROJ = SimpleNamespace(id=1, name="t")
_MSGS = [{"role": "user", "content": "hi"}]


def _run(messages=None, capability="chat:fast", response_format=None):
    return run_chat(
        project=_PROJ, capability=capability, messages=messages or _MSGS,
        model=None, max_tokens=1024, temperature=0.7,
        response_format=response_format, workflow=None,
    )


async def test_sync_chat_endpoint_returns_410_gone():
    """POST /v1/chat is removed — callers get a 410 with a migration hint to
    /v1/jobs (not a bare 404), for every capability incl. the old chat:deep."""
    plain, _ = await _make_project(["llm:chat"])
    for cap in ("chat:fast", "chat:smart", "chat:deep"):
        r = client.post(f"/v1/chat?capability={cap}",
                        headers={"X-Project-Key": plain},
                        json={"messages": _MSGS})
        assert r.status_code == 410, cap
        assert "/v1/jobs" in r.json()["detail"]


async def test_run_chat_none_when_no_key_available():
    """pick_and_reserve returns None for every provider → None (route → 503)."""
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=None)):
        assert await _run() is None


async def test_run_chat_happy_path_returns_outcome():
    fake_meta = {"model": "cerebras/gpt-oss-120b", "tokens_in": 12, "tokens_out": 8,
                 "cost_usd": 0.0, "latency_ms": 234}
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm",
                AsyncMock(return_value=("hello dima", fake_meta))), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        out = await _run()
    assert out is not None
    assert out.text == "hello dima"
    assert out.provider == "cerebras"
    assert out.tokens_in == 12
    assert out.key_label == "t"
    assert out.request_id == 101


async def test_run_chat_falls_back_on_cap_block():
    """reserve_cost raising CostGuardError → break to next provider."""
    from aibroker.routing import CostGuardError
    call_count = {"n": 0}

    async def fake_check(api_key, project, estimated_cost):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise CostGuardError(kind="key", limit=5.0, used=4.9, attempted=0.2)

    fake_meta = {"model": "groq/llama", "tokens_in": 1, "tokens_out": 1,
                 "cost_usd": 0.0, "latency_ms": 100}
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", side_effect=fake_check), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm",
                AsyncMock(return_value=("ok", fake_meta))), \
         patch("aibroker.services.llm_service.audit", AsyncMock()), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        out = await _run()
    assert out is not None
    assert call_count["n"] >= 2


async def test_run_chat_call_llm_failure_records_and_falls_back():
    call_count = {"n": 0}

    async def fake_call_llm(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("rate_limit hit")
        return ("recovered", {"model": "x", "tokens_in": 1, "tokens_out": 1,
                              "cost_usd": 0.0, "latency_ms": 10})

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.mark_cooldown", AsyncMock()) as cd, \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        out = await _run()
    assert out is not None
    cd.assert_awaited()


async def test_run_chat_auth_error_marks_key_dead():
    """401 from provider on every key → None (route → 503), mark_dead called."""
    async def fake_call_llm(*a, **kw):
        raise RuntimeError("401 unauthorized")

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.mark_dead", AsyncMock()) as md, \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        out = await _run()
    assert out is None
    md.assert_awaited()


async def test_run_chat_retries_multiple_keys_of_same_provider():
    """One rate-limited free key shouldn't sink the request — try the next key."""
    calls = {"n": 0}

    async def fake_call_llm(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("429 rate_limit hit")
        return ("recovered", {"model": "x", "tokens_in": 1, "tokens_out": 1,
                              "cost_usd": 0.0, "latency_ms": 1})

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.mark_cooldown", AsyncMock()), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        out = await _run()
    assert out is not None
    assert calls["n"] == 3


async def test_run_chat_invalid_json_falls_through_to_next_provider():
    """JSON request: a provider that returns unparseable JSON is skipped."""
    call_count = {"n": 0}

    async def fake_call_llm(*a, **kw):
        call_count["n"] += 1
        meta = {"model": "x", "tokens_in": 1, "tokens_out": 1,
                "cost_usd": 0.0, "latency_ms": 1}
        if call_count["n"] == 1:
            return ("{ broken json", meta)
        return ('{"ok": true}', meta)

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        out = await _run(response_format={"type": "json_object"})
    assert out is not None
    assert out.text == '{"ok": true}'
    assert call_count["n"] >= 2


# ─── /v1/embed ─────────────────────────────────────────────────────────────


async def test_embed_503_when_no_key():
    plain, _ = await _make_project(["llm:embed"])
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=None)):
        r = client.post(
            "/v1/embed?provider=voyage",
            headers={"X-Project-Key": plain},
            json={"input": ["hello"]},
        )
    assert r.status_code == 503


async def test_embed_happy_path():
    plain, _ = await _make_project(["llm:embed"])
    fake_meta = {
        "tokens_in": 5, "cost_usd": 0.0001, "latency_ms": 42,
    }
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.embed",
                AsyncMock(return_value=([[0.1, 0.2, 0.3]], fake_meta))), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/embed?provider=voyage",
            headers={"X-Project-Key": plain},
            json={"input": ["hello"]},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["embeddings"] == [[0.1, 0.2, 0.3]]
    assert data["provider"] == "voyage"
    assert data["tokens_in"] == 5
    assert data["request_id"] == 101


async def test_embed_502_on_provider_failure():
    plain, _ = await _make_project(["llm:embed"])

    async def fake_embed(*a, **kw):
        raise RuntimeError("boom")

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.embed", side_effect=fake_embed), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/embed?provider=voyage",
            headers={"X-Project-Key": plain},
            json={"input": ["hello"]},
        )
    assert r.status_code == 502
    assert "embed failed" in r.json()["detail"]


# ─── Scope guards ──────────────────────────────────────────────────────────


async def test_embed_requires_llm_embed_scope():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/embed?provider=voyage",
        headers={"X-Project-Key": plain},
        json={"input": ["x"]},
    )
    assert r.status_code == 403


# ─── Vision (multimodal chat content) ──────────────────────────────────────


async def test_run_chat_accepts_multimodal_content():
    """content as a list of blocks (text + image_url) reaches the provider
    unchanged — what media-worker sends for vision. Tests run_chat directly."""
    captured = {}

    async def fake_call_llm(*, model, messages, api_key, **kw):
        captured["messages"] = messages
        return "это кот на диване", {
            "model": model, "tokens_in": 10, "tokens_out": 5,
            "cost_usd": 0.0, "latency_ms": 30,
        }

    multimodal = [{"role": "user", "content": [
        {"type": "text", "text": "что на фото?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/xxx"}},
    ]}]
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        out = await _run(messages=multimodal, capability="vision")
    assert out is not None
    assert out.text == "это кот на диване"
    assert isinstance(captured["messages"][0]["content"], list)
    assert captured["messages"][0]["content"][0]["type"] == "text"


async def test_jobs_vision_requires_llm_vision_scope():
    """Scope guard for vision on the async endpoint (chat is async-only now)."""
    plain, _ = await _make_project(["llm:chat"])   # no llm:vision
    r = client.post("/v1/jobs?capability=vision",
                    headers={"X-Project-Key": plain},
                    json={"messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 403


# ─── Transcription (audio → text) ──────────────────────────────────────────


async def test_transcribe_requires_llm_audio_scope():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/transcribe",
        headers={"X-Project-Key": plain},
        files={"file": ("v.ogg", b"fakeaudio", "audio/ogg")},
    )
    assert r.status_code == 403


async def test_transcribe_rejects_empty_file():
    plain, _ = await _make_project(["llm:audio"])
    r = client.post(
        "/v1/transcribe",
        headers={"X-Project-Key": plain},
        files={"file": ("v.ogg", b"", "audio/ogg")},
    )
    assert r.status_code == 400


async def test_transcribe_503_when_no_key():
    plain, _ = await _make_project(["llm:audio"])
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=None)):
        r = client.post(
            "/v1/transcribe",
            headers={"X-Project-Key": plain},
            files={"file": ("v.ogg", b"fakeaudio", "audio/ogg")},
        )
    assert r.status_code == 503


async def test_transcribe_happy_path():
    plain, _ = await _make_project(["llm:audio"])
    fake_meta = {"model": "local/whisper",
                 "cost_usd": 0.0, "latency_ms": 120}

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.transcribe",
                AsyncMock(return_value=("привет это голосовое", fake_meta))), \
         patch("aibroker.services.llm_service.run_chat", AsyncMock(return_value=None)), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/transcribe?workflow=media",
            headers={"X-Project-Key": plain},
            files={"file": ("v.ogg", b"fakeaudiobytes", "audio/ogg")},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["text"] == "привет это голосовое"
    assert data["provider"] == "local"   # first in transcription chain (self-hosted ASR)
    assert data["request_id"] == 101


async def test_transcribe_local_applies_correction_pass():
    """The `local` provider's raw ASR text is proofread by chat:fast before it
    reaches the caller — the corrected text, not the raw transcript, must be
    what /v1/transcribe returns."""
    from aibroker.services.llm_service import ChatOutcome

    plain, _ = await _make_project(["llm:audio"])
    fake_meta = {"model": "local/whisper", "cost_usd": 0.0, "latency_ms": 120}
    corrected = ChatOutcome(
        text="Привет, это голосовое сообщение.", provider="gemini", model="gemini-2.5-flash",
        tokens_in=20, tokens_out=10, cost_usd=0.0, latency_ms=300,
        key_label="k", request_id=202,
    )
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.transcribe",
                AsyncMock(return_value=("привет ето галасовое сообщение", fake_meta))), \
         patch("aibroker.services.llm_service.run_chat", AsyncMock(return_value=corrected)), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/transcribe",
            headers={"X-Project-Key": plain},
            files={"file": ("v.ogg", b"fakeaudiobytes", "audio/ogg")},
        )
    assert r.status_code == 200
    assert r.json()["text"] == "Привет, это голосовое сообщение."


async def test_transcribe_502_when_all_providers_fail():
    plain, _ = await _make_project(["llm:audio"])

    async def boom(*a, **kw):
        raise RuntimeError("groq 500")

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.transcribe", side_effect=boom), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)), \
         patch("aibroker.services.llm_service._penalize", AsyncMock()):
        r = client.post(
            "/v1/transcribe",
            headers={"X-Project-Key": plain},
            files={"file": ("v.ogg", b"fakeaudiobytes", "audio/ogg")},
        )
    assert r.status_code == 502


# ─── chat:deep — async job API ──────────────────────────────────────────────


def test_next_poll_after_s_widens_over_time():
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC).replace(tzinfo=None)
    assert next_poll_after_s(now) == 5
    assert next_poll_after_s(now - timedelta(seconds=45)) == 10
    assert next_poll_after_s(now - timedelta(minutes=5)) == 20


async def test_deep_submit_requires_llm_deep_scope():
    plain, _ = await _make_project(["llm:chat"])  # no llm:deep
    r = client.post(
        "/v1/deep",
        headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403


async def test_deep_poll_404_for_unknown_job():
    plain, _ = await _make_project(["llm:deep"])
    r = client.get("/v1/deep/999999", headers={"X-Project-Key": plain})
    assert r.status_code == 404


@pytest.mark.skipif(ON_SQLITE, reason="cross-session read-after-write needs Postgres")
async def test_deep_poll_pending_job_returns_poll_after_s():
    """Insert a pending job directly (explicit id — BIGINT PK doesn't
    autoincrement on SQLite) to test the poll response shape without
    needing submit_deep_job's real insert-then-flush path."""
    plain, pid = await _make_project(["llm:deep"])
    async with get_session() as s:
        await s.execute(insert(DeepJobRow).values(
            id=5001, project_id=pid, status="pending",
            request={"messages": [], "model": None, "max_tokens": 4096,
                      "temperature": 0.7, "workflow": None},
        ))
    r = client.get("/v1/deep/5001", headers={"X-Project-Key": plain})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert data["poll_after_s"] == 5


@pytest.mark.skipif(ON_SQLITE, reason="cross-session read-after-write needs Postgres")
async def test_deep_poll_scoped_to_owning_project():
    """A job belonging to project A must 404 for project B — jobs are not a
    shared pool like keys, each belongs to exactly one caller."""
    plain_a, pid_a = await _make_project(["llm:deep"])
    plain_b, _ = await _make_project(["llm:deep"])
    async with get_session() as s:
        await s.execute(insert(DeepJobRow).values(
            id=5002, project_id=pid_a, status="done",
            request={"messages": []}, result_text="secret answer",
        ))
    r = client.get("/v1/deep/5002", headers={"X-Project-Key": plain_b})
    assert r.status_code == 404
    r_owner = client.get("/v1/deep/5002", headers={"X-Project-Key": plain_a})
    assert r_owner.status_code == 200
    assert r_owner.json()["text"] == "secret answer"


@pytest.mark.skipif(ON_SQLITE, reason="cross-session read-after-write needs Postgres")
async def test_deep_poll_done_job_returns_result_meta():
    plain, pid = await _make_project(["llm:deep"])
    async with get_session() as s:
        await s.execute(insert(DeepJobRow).values(
            id=5003, project_id=pid, status="done",
            request={"messages": []}, result_text="the answer",
            result_meta={"provider": "nvidia", "model": "nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
                          "tokens_in": 50, "tokens_out": 20, "cost_usd": 0.0,
                          "latency_ms": 98000, "key_label": "demoniwwwe",
                          "request_id": 999, "cache_read_tokens": 0, "cache_write_tokens": 0},
        ))
    r = client.get("/v1/deep/5003", headers={"X-Project-Key": plain})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "done"
    assert data["text"] == "the answer"
    assert data["provider"] == "nvidia"
    assert data["latency_ms"] == 98000


@pytest.mark.skipif(ON_SQLITE, reason="cross-session read-after-write needs Postgres")
async def test_deep_poll_error_job_returns_error_message():
    plain, pid = await _make_project(["llm:deep"])
    async with get_session() as s:
        await s.execute(insert(DeepJobRow).values(
            id=5004, project_id=pid, status="error",
            request={"messages": []},
            error_message="no provider available for capability=chat:deep",
        ))
    r = client.get("/v1/deep/5004", headers={"X-Project-Key": plain})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "error"
    assert "no provider available" in data["error"]


@pytest.mark.skipif(ON_SQLITE, reason="cross-session read-after-write needs Postgres")
async def test_deep_poll_long_pending_job_stays_pending():
    """REGRESSION (2026-07-10): a long-pending job is NOT failed by poll — poll
    is a pure read. get_job used to flip a >20-min pending row to error, which
    raced the dispatcher and killed jobs still legitimately retrying under
    backoff. The dispatcher (job_queue) owns give-up now (after _MAX_RETRIES)."""
    from datetime import UTC, datetime, timedelta
    plain, pid = await _make_project(["llm:deep"])
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=45)
    async with get_session() as s:
        await s.execute(insert(DeepJobRow).values(
            id=5005, project_id=pid, status="pending",
            request={"messages": []}, created_at=old,
        ))
    r = client.get("/v1/deep/5005", headers={"X-Project-Key": plain})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "pending"
    assert data["poll_after_s"] is not None
    # unchanged in the DB — poll wrote nothing
    async with get_session() as s:
        row = await s.get(DeepJobRow, 5005)
        assert row.status == "pending"
        assert row.error_message is None


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL autoincrement needs Postgres")
async def test_deep_submit_enqueues_and_dispatcher_drains_to_done():
    """Full path on real Postgres: submit ENQUEUES a pending row (202) →
    drain_once() (the deterministic one dispatcher wave) claims + runs it
    (mocked run_chat) → poll sees done. We drive drain_once() explicitly
    instead of racing the lifespan's background loop through TestClient's
    portal (a cross-event-loop timing dance); prod runs the same claim/execute
    code from dispatcher_loop on a normal uvicorn loop."""
    from aibroker.services.job_queue import drain_once
    from aibroker.services.llm_service import ChatOutcome

    plain, _ = await _make_project(["llm:deep"])
    r = client.post(
        "/v1/deep", headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.json()["poll_url"] == f"/v1/deep/{job_id}"

    fake_outcome = ChatOutcome(
        text="deep answer", provider="nvidia",
        model="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
        tokens_in=40, tokens_out=15, cost_usd=0.0, latency_ms=45000,
        key_label="demoniwwwe", request_id=777,
    )
    with patch("aibroker.services.job_queue.run_chat",
                AsyncMock(return_value=fake_outcome)):
        assert await drain_once() == 1

    poll = client.get(f"/v1/deep/{job_id}", headers={"X-Project-Key": plain})
    assert poll.status_code == 200
    data = poll.json()
    assert data["status"] == "done"
    assert data["text"] == "deep answer"
    assert data["provider"] == "nvidia"
    assert data["request_id"] == 777


# ─── generic async jobs — POST /v1/jobs (Phase 4) ─────────────────────────────


async def test_jobs_submit_rejects_non_job_capability():
    """embedding/transcription are sync-only; unknown capabilities too. The
    async job API serves only run_chat capabilities."""
    plain, _ = await _make_project(["llm:chat", "llm:embed"])
    for cap in ("embedding", "transcription", "chat:nonsense"):
        r = client.post(
            f"/v1/jobs?capability={cap}",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 400, cap


async def test_jobs_submit_requires_capability_scope():
    plain, _ = await _make_project(["llm:embed"])  # no llm:chat
    r = client.post(
        "/v1/jobs?capability=chat:fast",
        headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403


async def test_jobs_submit_accepts_chat_sales_as_a_job_capability():
    """chat:sales (Stepan2's Sonnet-first sales lane) is a real async-job
    capability gated on llm:chat — a project WITHOUT llm:chat is rejected at
    the scope gate (403), not bounced as an unknown capability (400). Proves
    the route recognizes it and gates it on the shared llm:chat scope."""
    plain, _ = await _make_project(["llm:embed"])  # no llm:chat
    r = client.post(
        "/v1/jobs?capability=chat:sales",
        headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403  # gated on scope, NOT 400 unknown-capability


async def test_jobs_poll_404_for_unknown_job():
    plain, _ = await _make_project(["llm:chat"])
    r = client.get("/v1/jobs/999999", headers={"X-Project-Key": plain})
    assert r.status_code == 404


async def test_jobs_submit_rejects_oversized_max_tokens():
    """An unbounded max_tokens inflated the cost-guard's worst-case reservation
    and silently knocked every capped paid key out of the chain (2026-07-16) —
    the request must be rejected at the schema, not starve the paid tail."""
    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/jobs?capability=chat:fast",
        headers={"X-Project-Key": plain},
        json={"messages": _MSGS, "max_tokens": 1_000_000},
    )
    assert r.status_code == 422


async def test_jobs_submit_rejects_out_of_range_temperature():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/jobs?capability=chat:fast",
        headers={"X-Project-Key": plain},
        json={"messages": _MSGS, "temperature": 5},
    )
    assert r.status_code == 422


async def test_deep_submit_rejects_oversized_max_tokens():
    """DeepRequest has its own (higher) ceiling — but still a ceiling."""
    plain, _ = await _make_project(["llm:deep"])
    r = client.post(
        "/v1/deep",
        headers={"X-Project-Key": plain},
        json={"messages": _MSGS, "max_tokens": 1_000_000},
    )
    assert r.status_code == 422


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL autoincrement needs Postgres")
async def test_jobs_submit_creates_job_for_chat_fast_and_runs():
    """Generic async path on Postgres: submit chat:fast (with response_format)
    → drain_once() claims + runs it (mocked run_chat) → poll sees done. Confirms
    the capability + response_format are threaded through the queue."""
    from aibroker.services.job_queue import drain_once
    from aibroker.services.llm_service import ChatOutcome

    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/jobs?capability=chat:fast", headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "hi"}],
              "response_format": {"type": "json_object"}},
    )
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.json()["poll_url"] == f"/v1/jobs/{job_id}"

    captured: dict = {}

    async def fake_run_chat(**kw):
        captured.update(kw)
        return ChatOutcome(
            text='{"ok":true}', provider="cerebras", model="cerebras/gpt-oss-120b",
            tokens_in=10, tokens_out=3, cost_usd=0.0, latency_ms=800,
            key_label="eatmeat", request_id=888,
        )

    with patch("aibroker.services.job_queue.run_chat", fake_run_chat):
        assert await drain_once() == 1

    poll = client.get(f"/v1/jobs/{job_id}", headers={"X-Project-Key": plain})
    assert poll.json()["status"] == "done"
    assert poll.json()["text"] == '{"ok":true}'
    # capability + response_format actually threaded through to run_chat
    assert captured["capability"] == "chat:fast"
    assert captured["response_format"] == {"type": "json_object"}


# ─── _job_response status mapping (regression: running → pending, not empty done) ──

def _fake_job_row(**kw):
    from datetime import UTC, datetime
    base = {
        "id": 7, "status": "pending", "result_text": None, "result_meta": None,
        "error_message": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
    }
    base.update(kw)
    return SimpleNamespace(**base)


def test_job_response_running_maps_to_pending_not_empty_done():
    """REGRESSION: the queue dispatcher claims a job as `running` while it
    executes — a state the old fire-and-forget path never had. _job_response
    must treat it as pending, else it falls through to status=done with
    text=null and the client stops polling on an empty answer."""
    from aibroker.routes.proxy import _job_response
    resp = _job_response(_fake_job_row(status="running"))
    assert resp.status == "pending"
    assert resp.text is None
    assert resp.poll_after_s is not None


def test_job_response_pending_stays_pending():
    from aibroker.routes.proxy import _job_response
    resp = _job_response(_fake_job_row(status="pending"))
    assert resp.status == "pending"
    assert resp.poll_after_s is not None


def test_job_response_unknown_status_defaults_to_pending():
    """Fail-safe: any unexpected status keeps the client polling rather than
    handing back an empty done."""
    from aibroker.routes.proxy import _job_response
    assert _job_response(_fake_job_row(status="queued_weird")).status == "pending"


def test_job_response_done_returns_result():
    from aibroker.routes.proxy import _job_response
    row = _fake_job_row(
        status="done", result_text="hello",
        result_meta={"provider": "cerebras", "model": "m", "tokens_in": 5,
                     "tokens_out": 2, "cost_usd": 0.0, "latency_ms": 100,
                     "key_label": "k", "request_id": 1},
    )
    resp = _job_response(row)
    assert resp.status == "done"
    assert resp.text == "hello"
    assert resp.provider == "cerebras"


def test_job_response_error_returns_message():
    from aibroker.routes.proxy import _job_response
    resp = _job_response(_fake_job_row(status="error", error_message="boom"))
    assert resp.status == "error"
    assert resp.error == "boom"
