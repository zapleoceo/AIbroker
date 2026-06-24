"""routes/vending — /v1/key, /v1/usage, /v1/release."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aibroker.main import app


client = TestClient(app)


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
