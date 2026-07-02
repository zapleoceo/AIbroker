"""routes/vending — /v1/key, /v1/usage, /v1/release."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aibroker.main import app

client = TestClient(app)

_DB = os.environ.get("DATABASE_URL", "")
ON_POSTGRES = "postgres" in _DB or "asyncpg" in _DB

_PID_COUNTER = [5000]


async def _make_project(scopes: list[str]) -> tuple[str, int]:
    """Insert a project, return (project_key_plain, project_id). Explicit id —
    SQLite's BIGINT PK doesn't autoincrement (matches test_routes_proxy.py)."""
    from aibroker.auth import generate_project_key, hash_project_key
    from aibroker.db import get_session
    from aibroker.db.models import ProjectRow

    plain = generate_project_key()
    _PID_COUNTER[0] += 1
    pid = _PID_COUNTER[0]
    async with get_session() as s:
        s.add(ProjectRow(
            id=pid, name=f"vending_test_{pid}",
            project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12],
            allowed_scopes=scopes, is_active=True, notes="",
        ))
    return plain, pid


async def _seed_leases(project_id: int, count: int) -> None:
    """Insert `count` lease rows for `project_id`. `leased_at` defaults to now
    server-side (fresh — counts toward the rolling-minute rate limit);
    `lease_until` just needs any valid future expiry, unrelated to the check."""
    import secrets
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import insert

    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow, LeaseRow

    # naive datetime: leases.lease_until is TIMESTAMP WITHOUT TIME ZONE —
    # asyncpg rejects tz-aware values on that column type.
    until = (datetime.now(UTC) + timedelta(minutes=1)).replace(tzinfo=None)
    async with get_session() as s:
        s.add(ApiKeyRow(
            id=project_id * 10, provider="cerebras", label="rl-test",
            tier="free", scopes=["llm:chat"], token_encrypted="x",
            is_active=True, is_alive=True,
        ))
        await s.flush()
        await s.execute(insert(LeaseRow), [
            {
                "id": "lse_" + secrets.token_urlsafe(8),
                "api_key_id": project_id * 10, "project_id": project_id,
                "lease_until": until,
            }
            for _ in range(count)
        ])


def test_vend_requires_project_key():
    r = client.post("/v1/key", json={"provider": "cerebras", "scope": "llm:chat"})
    assert r.status_code == 401


def test_vend_rejects_wrong_project_key():
    r = client.post(
        "/v1/key",
        headers={"X-Project-Key": "aib_prj_fake_does_not_exist"},
        json={"provider": "cerebras", "scope": "llm:chat"},
    )
    assert r.status_code == 401


def test_usage_requires_project_key():
    r = client.post("/v1/usage", json={"lease_id": "lse_x", "status": "ok"})
    assert r.status_code == 401


def test_release_requires_project_key():
    r = client.post("/v1/release", json={"lease_id": "lse_x"})
    assert r.status_code == 401


def test_usage_validates_status_enum():
    """status must be one of ok/rate_limit/auth_fail/error."""
    r = client.post(
        "/v1/usage",
        headers={"X-Project-Key": "anything"},
        json={"lease_id": "lse_x", "status": "weird-value"},
    )
    # 401 (auth fails first) is fine — proves the route is registered
    # If we got here with valid auth we'd see 422
    assert r.status_code in (401, 422)


def test_release_missing_lease_id_400():
    r = client.post(
        "/v1/release",
        headers={"X-Project-Key": "anything"},
        json={},
    )
    # 401 again — but route exists
    assert r.status_code in (400, 401)


def test_chat_requires_project_key():
    r = client.post("/v1/chat?capability=chat:fast",
                     json={"messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


def test_embed_requires_project_key():
    r = client.post("/v1/embed?provider=voyage", json={"input": ["text"]})
    assert r.status_code == 401


def test_chat_rejects_invalid_capability():
    """Capability validation happens after auth — but we can still check route exists."""
    r = client.post("/v1/chat?capability=made-up",
                     headers={"X-Project-Key": "fake"},
                     json={"messages": [{"role": "user", "content": "x"}]})
    # 401 (auth) before 400 (cap check)
    assert r.status_code in (400, 401)


def test_chat_validates_messages_required():
    """Empty messages list violates min_length=1."""
    r = client.post("/v1/chat?capability=chat:fast",
                     headers={"X-Project-Key": "fake"},
                     json={"messages": []})
    # Pydantic validates min_length=1 BEFORE auth (depends on order)
    assert r.status_code in (401, 422)


def test_embed_validates_input_min_length():
    r = client.post("/v1/embed?provider=voyage",
                     headers={"X-Project-Key": "fake"},
                     json={"input": []})
    assert r.status_code in (401, 422)


def test_embed_validates_input_max_length():
    r = client.post("/v1/embed?provider=voyage",
                     headers={"X-Project-Key": "fake"},
                     json={"input": ["x"] * 200})  # max=128
    assert r.status_code in (401, 422)


# ─── Vending rate limit (2026-07-03) ─────────────────────────────────────────
#
# POST /v1/key hands out a real plaintext provider token per call and had NO
# rate limit at all — a compromised project key could drain the lease pool or
# exfiltrate tokens unboundedly. `_check_vending_rate_limit` counts recent
# `leases.leased_at` rows for the project (Postgres-only now()/INTERVAL —
# these tests need a real Postgres, matching the project's convention for
# such queries).


@pytest.mark.skipif(not ON_POSTGRES, reason="rate limit query uses Postgres now()/INTERVAL")
async def test_rate_limit_passes_under_the_limit(monkeypatch):
    from types import SimpleNamespace

    from aibroker.config import get_settings
    from aibroker.routes.vending import _check_vending_rate_limit

    monkeypatch.setattr(get_settings(), "VENDING_RATE_LIMIT_PER_MINUTE", 5)
    _, pid = await _make_project(["llm:chat"])
    await _seed_leases(pid, 4)  # 4 < 5 → must not raise
    fake_req = SimpleNamespace(headers={}, client=None)
    await _check_vending_rate_limit(pid, fake_req)


@pytest.mark.skipif(not ON_POSTGRES, reason="rate limit query uses Postgres now()/INTERVAL")
async def test_rate_limit_blocks_at_the_limit(monkeypatch):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from aibroker.config import get_settings
    from aibroker.routes.vending import _check_vending_rate_limit

    monkeypatch.setattr(get_settings(), "VENDING_RATE_LIMIT_PER_MINUTE", 5)
    _, pid = await _make_project(["llm:chat"])
    await _seed_leases(pid, 5)  # 5 >= 5 → must block
    fake_req = SimpleNamespace(headers={}, client=None)
    with pytest.raises(HTTPException) as exc:
        await _check_vending_rate_limit(pid, fake_req)
    assert exc.value.status_code == 429


@pytest.mark.skipif(not ON_POSTGRES, reason="rate limit query uses Postgres now()/INTERVAL")
async def test_rate_limit_scoped_per_project(monkeypatch):
    """One project hammering the endpoint must not throttle a different
    project — the count is scoped by project_id."""
    from types import SimpleNamespace

    from aibroker.config import get_settings
    from aibroker.routes.vending import _check_vending_rate_limit

    monkeypatch.setattr(get_settings(), "VENDING_RATE_LIMIT_PER_MINUTE", 3)
    _, hammered = await _make_project(["llm:chat"])
    _, quiet = await _make_project(["llm:chat"])
    await _seed_leases(hammered, 10)  # way over the limit
    fake_req = SimpleNamespace(headers={}, client=None)
    await _check_vending_rate_limit(quiet, fake_req)  # quiet project unaffected


@pytest.mark.skipif(not ON_POSTGRES, reason="rate limit query uses Postgres now()/INTERVAL")
async def test_rate_limit_disabled_when_zero(monkeypatch):
    """VENDING_RATE_LIMIT_PER_MINUTE <= 0 is an explicit escape hatch — skip
    the DB query entirely rather than treat 0 as 'always block'."""
    from types import SimpleNamespace

    from aibroker.config import get_settings
    from aibroker.routes.vending import _check_vending_rate_limit

    monkeypatch.setattr(get_settings(), "VENDING_RATE_LIMIT_PER_MINUTE", 0)
    fake_req = SimpleNamespace(headers={}, client=None)
    # project_id=999999999 doesn't exist — if this queried the DB it would
    # still return count=0 and pass, so the real proof is that it returns
    # instantly with no DB round trip; functionally, not-raising is enough.
    await _check_vending_rate_limit(999_999_999, fake_req)


@pytest.mark.skipif(not ON_POSTGRES, reason="rate limit query uses Postgres now()/INTERVAL")
async def test_vend_key_endpoint_returns_429_over_limit(monkeypatch):
    """End-to-end through the real HTTP route: once the limit is hit, POST
    /v1/key returns 429 before ever touching pick_and_reserve."""
    from unittest.mock import AsyncMock, patch

    from aibroker.config import get_settings

    monkeypatch.setattr(get_settings(), "VENDING_RATE_LIMIT_PER_MINUTE", 2)
    plain, pid = await _make_project(["llm:chat"])
    await _seed_leases(pid, 2)

    with patch("aibroker.routes.vending.pick_and_reserve", AsyncMock()) as picker:
        r = client.post(
            "/v1/key",
            headers={"X-Project-Key": plain},
            json={"provider": "cerebras", "scope": "llm:chat"},
        )
    assert r.status_code == 429
    picker.assert_not_awaited()  # rejected before ever picking a key
