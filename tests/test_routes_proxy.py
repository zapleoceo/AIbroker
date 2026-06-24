"""routes/proxy — /v1/chat and /v1/embed happy paths + fallback + errors.

All tests mock `pick_and_reserve` + `call_llm` + `embed` + `record_usage`
so they don't need real Postgres / real LLM providers.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from aibroker.auth import generate_project_key, hash_project_key
from aibroker.crypto import encrypt
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.main import app


client = TestClient(app)


# ─── Fixture: in-DB project + key (uses default test fixture's SQLite) ───


async def _make_project(scopes: list[str]) -> tuple[str, int]:
    """Insert a project, return (project_key_plain, project_id)."""
    plain = generate_project_key()
    async with get_session() as s:
        result = await s.execute(insert(ProjectRow).values(
            name=f"proxy_test_{id(scopes)}",
            project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12],
            allowed_scopes=scopes,
            is_active=True,
            notes="",
        ).returning(ProjectRow.id))
        pid = result.scalar_one()
    return plain, pid


def _fake_key():
    """A fake ApiKeyRow object (not from DB) suitable for proxy logic."""
    return ApiKeyRow(
        id=1, provider="cerebras", label="t",
        token_encrypted=encrypt("fake-token"),
        tier="free", scopes=["llm:chat"],
        is_active=True, is_alive=True,
        error_count=0,
        cost_today_usd=0.0,
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
    with patch("aibroker.routes.proxy.pick_and_reserve",
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
    with patch("aibroker.routes.proxy.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.routes.proxy.check_caps", AsyncMock()), \
         patch("aibroker.routes.proxy.call_llm",
                AsyncMock(return_value=("hello dima", fake_meta))), \
         patch("aibroker.routes.proxy.record_usage", AsyncMock()):
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


async def test_chat_falls_back_on_cap_block():
    """When check_caps raises, _try_one_provider returns None → next provider tried."""
    from aibroker.routing import CostGuardError
    plain, _ = await _make_project(["llm:chat"])
    call_count = {"n": 0}

    async def fake_check(api_key, project, estimated_cost):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise CostGuardError("cap exceeded")

    fake_meta = {
        "model": "groq/llama", "tokens_in": 1, "tokens_out": 1,
        "cost_usd": 0.0, "latency_ms": 100,
    }

    with patch("aibroker.routes.proxy.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.routes.proxy.check_caps", side_effect=fake_check), \
         patch("aibroker.routes.proxy.call_llm",
                AsyncMock(return_value=("ok", fake_meta))), \
         patch("aibroker.routes.proxy.audit", AsyncMock()), \
         patch("aibroker.routes.proxy.record_usage", AsyncMock()):
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

    with patch("aibroker.routes.proxy.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.routes.proxy.check_caps", AsyncMock()), \
         patch("aibroker.routes.proxy.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.routes.proxy.mark_cooldown", AsyncMock()) as cd, \
         patch("aibroker.routes.proxy.record_usage", AsyncMock()):
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

    with patch("aibroker.routes.proxy.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.routes.proxy.check_caps", AsyncMock()), \
         patch("aibroker.routes.proxy.call_llm", side_effect=fake_call_llm), \
         patch("aibroker.routes.proxy.mark_dead", AsyncMock()) as md, \
         patch("aibroker.routes.proxy.record_usage", AsyncMock()):
        r = client.post(
            "/v1/chat?capability=chat:fast",
            headers={"X-Project-Key": plain},
            json={"messages": [{"role": "user", "content": "hi"}]},
        )
    assert r.status_code == 503  # all providers errored
    md.assert_awaited()


# ─── /v1/embed ─────────────────────────────────────────────────────────────


async def test_embed_503_when_no_key():
    plain, _ = await _make_project(["llm:embed"])
    with patch("aibroker.routes.proxy.pick_and_reserve",
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
    with patch("aibroker.routes.proxy.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.routes.proxy.embed",
                AsyncMock(return_value=([[0.1, 0.2, 0.3]], fake_meta))), \
         patch("aibroker.routes.proxy.record_usage", AsyncMock()):
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


async def test_embed_502_on_provider_failure():
    plain, _ = await _make_project(["llm:embed"])

    async def fake_embed(*a, **kw):
        raise RuntimeError("boom")

    with patch("aibroker.routes.proxy.pick_and_reserve",
                AsyncMock(return_value=_fake_key())), \
         patch("aibroker.routes.proxy.embed", side_effect=fake_embed), \
         patch("aibroker.routes.proxy.record_usage", AsyncMock()):
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
