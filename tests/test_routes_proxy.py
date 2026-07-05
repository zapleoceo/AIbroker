"""routes/proxy — /v1/chat and /v1/embed happy paths + fallback + errors.

All tests mock `pick_and_reserve` + `call_llm` + `embed` + `record_usage`
so they don't need real Postgres / real LLM providers.
"""
from __future__ import annotations

import os
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


# ─── /v1/chat ──────────────────────────────────────────────────────────────


async def test_chat_validates_capability():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/chat?capability=made-up",
        headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "x"}]},
    )
    assert r.status_code == 400
    assert "unknown capability" in r.json()["detail"]


async def test_chat_503_when_no_key_available():
    """pick_and_reserve returns None for every provider → 503."""
    plain, _ = await _make_project(["llm:chat"])
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=None)):
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 503
    assert "no provider available" in r.json()["detail"]


async def test_chat_happy_path_returns_response():
    plain, _ = await _make_project(["llm:chat"])
    fake_meta = {
        "model": "cerebras/gpt-oss-120b",
        "tokens_in": 12, "tokens_out": 8,
        "cost_usd": 0.0, "latency_ms": 234,
    }
    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm",
                AsyncMock(return_value=("hello dima", fake_meta))), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["text"] == "hello dima"
    assert data["provider"] == "cerebras"
    assert data["tokens_in"] == 12
    assert data["tokens_out"] == 8
    assert data["key_label"] == "t"  # surfaced for the Stepan UI chip
    assert data["request_id"] == 101  # usage_log.id — caller correlates own logs


async def test_chat_falls_back_on_cap_block():
    """When check_caps raises, _try_one_provider returns None → next provider tried."""
    from aibroker.routing import CostGuardError
    plain, _ = await _make_project(["llm:chat"])
    call_count = {"n": 0}

    async def fake_check(api_key, project, estimated_cost):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise CostGuardError(kind="key", limit=5.0, used=4.9, attempted=0.2)

    fake_meta = {
        "model": "groq/llama", "tokens_in": 1, "tokens_out": 1,
        "cost_usd": 0.0, "latency_ms": 100,
    }

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", side_effect=fake_check), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm",
                AsyncMock(return_value=("ok", fake_meta))), \
         patch("aibroker.services.llm_service.audit", AsyncMock()), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert call_count["n"] >= 2  # at least one fallback attempted


async def test_chat_call_llm_failure_records_and_falls_back():
    plain, _ = await _make_project(["llm:chat"])
    call_count = {"n": 0}

    async def fake_call_llm(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("rate_limit hit")
        return ("recovered", {
            "model": "x", "tokens_in": 1, "tokens_out": 1,
            "cost_usd": 0.0, "latency_ms": 10,
        })

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.mark_cooldown", AsyncMock()) as cd, \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    cd.assert_awaited()  # rate_limit triggered cooldown


async def test_chat_auth_error_marks_key_dead():
    """401 from provider → mark_dead is called."""
    plain, _ = await _make_project(["llm:chat"])

    async def fake_call_llm(*a, **kw):
        raise RuntimeError("401 unauthorized")

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.mark_dead", AsyncMock()) as md, \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 503  # all providers errored
    md.assert_awaited()


async def test_chat_retries_multiple_keys_of_same_provider():
    """One rate-limited free key shouldn't sink the request — try the next key."""
    plain, _ = await _make_project(["llm:chat"])
    calls = {"n": 0}

    async def fake_call_llm(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:                       # first two keys are rate-limited
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
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 200
    assert calls["n"] == 3  # tried 3 keys of the same provider before success


async def test_chat_invalid_json_falls_through_to_next_provider():
    """JSON request: a provider that returns unparseable JSON is skipped."""
    plain, _ = await _make_project(["llm:chat"])
    call_count = {"n": 0}

    async def fake_call_llm(*a, **kw):
        call_count["n"] += 1
        meta = {"model": "x", "tokens_in": 1, "tokens_out": 1,
                "cost_usd": 0.0, "latency_ms": 1}
        if call_count["n"] == 1:
            return ("{ broken json", meta)   # unparseable → skip
        return ('{"ok": true}', meta)        # valid → win

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}],
                  "response_format": {"type": "json_object"}},
        )
    assert r.status_code == 200
    assert r.json()["text"] == '{"ok": true}'
    assert call_count["n"] >= 2  # first (broken JSON) was skipped


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


async def test_chat_requires_llm_chat_scope():
    """Project with only embed scope → 403 on /v1/chat."""
    plain, _ = await _make_project(["llm:embed"])
    r = client.post(
        "/v1/chat?capability=chat:fast",
        headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403


async def test_embed_requires_llm_embed_scope():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/embed?provider=voyage",
        headers={"X-Project-Key": plain},
        json={"input": ["x"]},
    )
    assert r.status_code == 403


# ─── Vision (multimodal chat content) ──────────────────────────────────────


async def test_chat_accepts_multimodal_content():
    """content as a list of blocks (text + image_url) must validate and reach
    the provider unchanged — this is what media-worker sends for vision."""
    plain, _ = await _make_project(["llm:vision"])
    captured = {}

    async def fake_call_llm(*, model, messages, api_key, **kw):
        captured["messages"] = messages
        return "это кот на диване", {
            "model": model, "tokens_in": 10, "tokens_out": 5,
            "cost_usd": 0.0, "latency_ms": 30,
        }

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.reserve_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.release_cost", AsyncMock()), \
         patch("aibroker.services.llm_service.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/chat?capability=vision",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": [
                {"type": "text", "text": "что на фото?"},
                {"type": "image_url",
                 "image_url": {"url": "data:image/jpeg;base64,/9j/xxx"}},
            ]}]},
        )
    assert r.status_code == 200
    assert r.json()["text"] == "это кот на диване"
    # multimodal list survived round-trip to the provider call
    assert isinstance(captured["messages"][0]["content"], list)
    assert captured["messages"][0]["content"][0]["type"] == "text"


async def test_chat_vision_requires_llm_vision_scope():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post(
        "/v1/chat?capability=vision",
        headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "x"}]},
    )
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
    fake_meta = {"model": "groq/whisper-large-v3-turbo",
                 "cost_usd": 0.0, "latency_ms": 120}

    with patch("aibroker.services.llm_service.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.services.llm_service.transcribe",
                AsyncMock(return_value=("привет это голосовое", fake_meta))), \
         patch("aibroker.services.llm_service.record_usage", AsyncMock(return_value=101)):
        r = client.post(
            "/v1/transcribe?workflow=media",
            headers={"X-Project-Key": plain},
            files={"file": ("v.ogg", b"fakeaudiobytes", "audio/ogg")},
        )
    assert r.status_code == 200
    data = r.json()
    assert data["text"] == "привет это голосовое"
    assert data["provider"] == "groq"   # first in transcription chain
    assert data["request_id"] == 101


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


async def test_chat_deep_rejected_on_sync_chat_endpoint():
    """chat:deep must never be reachable via the synchronous /v1/chat — its
    real latency (up to ~8 min observed) exceeds Cloudflare's and this
    broker's own nginx read timeouts, so a blocking call here would 504
    the caller while the broker is still waiting on the provider."""
    plain, _ = await _make_project(["llm:deep"])
    r = client.post(
        "/v1/chat?capability=chat:deep",
        headers={"X-Project-Key": plain},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400
    assert "async-only" in r.json()["detail"]
    assert "/v1/deep" in r.json()["detail"]


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


async def test_deep_poll_stale_pending_job_times_out():
    """A job stuck 'pending' past _STALE_AFTER_S (worker restarted mid-call)
    resolves to a timeout error on poll instead of hanging forever."""
    from datetime import UTC, datetime, timedelta
    plain, pid = await _make_project(["llm:deep"])
    old = datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=25)
    async with get_session() as s:
        await s.execute(insert(DeepJobRow).values(
            id=5005, project_id=pid, status="pending",
            request={"messages": []}, created_at=old,
        ))
    r = client.get("/v1/deep/5005", headers={"X-Project-Key": plain})
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "error"
    assert "timed out" in data["error"]


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL autoincrement needs Postgres")
async def test_deep_submit_creates_job_and_runs_in_background():
    """Full loop on real Postgres: submit → background task runs (mocked
    run_chat) → poll sees status=done with the result."""
    import asyncio

    from aibroker.services.llm_service import ChatOutcome

    plain, _ = await _make_project(["llm:deep"])
    fake_outcome = ChatOutcome(
        text="deep answer", provider="nvidia",
        model="nvidia_nim/nvidia/nemotron-3-ultra-550b-a55b",
        tokens_in=40, tokens_out=15, cost_usd=0.0, latency_ms=45000,
        key_label="demoniwwwe", request_id=777,
    )
    with patch("aibroker.services.deep_jobs.run_chat",
                AsyncMock(return_value=fake_outcome)):
        r = client.post(
            "/v1/deep",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 202
        job_id = r.json()["job_id"]
        assert r.json()["poll_url"] == f"/v1/deep/{job_id}"

        # Background task runs on the same event loop as this test — give it
        # a beat to complete before polling.
        for _ in range(20):
            await asyncio.sleep(0.05)
            poll = client.get(f"/v1/deep/{job_id}", headers={"X-Project-Key": plain})
            if poll.json()["status"] != "pending":
                break

    assert poll.status_code == 200
    data = poll.json()
    assert data["status"] == "done"
    assert data["text"] == "deep answer"
    assert data["provider"] == "nvidia"
    assert data["request_id"] == 777
