"""routes/admin — X-Admin-Key gated CRUD."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aibroker.config import get_settings
from aibroker.main import app


client = TestClient(app)
ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")
ADMIN = "admin_test_key_for_ci_only"


def _admin_headers():
    return {"X-Admin-Key": get_settings().ADMIN_KEY}


# ─── Auth ───────────────────────────────────────────────────────────────────


def test_admin_routes_require_key():
    for path, body in [
        ("/admin/projects", {}),
        ("/admin/keys", {}),
    ]:
        r = client.get(path)
        assert r.status_code == 401, f"GET {path} should require X-Admin-Key"


def test_admin_routes_reject_wrong_key():
    r = client.get("/admin/projects", headers={"X-Admin-Key": "totally wrong"})
    assert r.status_code == 401


def test_list_projects_empty():
    r = client.get("/admin/projects", headers=_admin_headers())
    assert r.status_code == 200
    assert r.json() == []


def test_list_keys_empty():
    r = client.get("/admin/keys", headers=_admin_headers())
    assert r.status_code == 200
    assert r.json() == []


# ─── Projects CRUD (needs DB writes) ────────────────────────────────────────


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres for SQLite autoincrement")
def test_create_project_returns_one_time_key():
    payload = {
        "name": "test_project",
        "owner_email": "x@y",
        "allowed_scopes": ["llm:chat", "llm:embed"],
        "daily_cost_cap_usd": 5.0,
    }
    r = client.post("/admin/projects", headers=_admin_headers(), json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["name"] == "test_project"
    assert "project_key" in data
    assert data["project_key"].startswith("aib_prj_")
    assert data["allowed_scopes"] == ["llm:chat", "llm:embed"]


def test_create_project_validates_name():
    """Lowercase + [a-z0-9_-]* only."""
    bad = {"name": "Invalid Caps", "allowed_scopes": ["llm:chat"]}
    r = client.post("/admin/projects", headers=_admin_headers(), json=bad)
    assert r.status_code == 422   # Pydantic validation


def test_create_project_rejects_too_short_name():
    bad = {"name": "x", "allowed_scopes": ["llm:chat"]}
    r = client.post("/admin/projects", headers=_admin_headers(), json=bad)
    assert r.status_code == 422


# ─── Keys CRUD ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
def test_create_key_via_admin():
    payload = {
        "provider": "cerebras",
        "label": "test_key",
        "token": "csk-fake-test-token-12345",
        "tier": "free",
        "scopes": ["llm:chat"],
    }
    r = client.post("/admin/keys", headers=_admin_headers(), json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "cerebras"
    assert data["tier"] == "free"
    assert data["is_active"] is True


def test_create_key_validates_tier():
    bad = {
        "provider": "x", "label": "y",
        "token": "long-enough-token-here",
        "tier": "lifetime",   # not in {free, paid, trial}
    }
    r = client.post("/admin/keys", headers=_admin_headers(), json=bad)
    assert r.status_code == 422


def test_create_key_token_min_length():
    bad = {"provider": "x", "label": "y", "token": "tiny"}
    r = client.post("/admin/keys", headers=_admin_headers(), json=bad)
    assert r.status_code == 422


def test_disable_nonexistent_key_404():
    r = client.post("/admin/keys/99999/disable", headers=_admin_headers())
    assert r.status_code == 404


def test_delete_nonexistent_key_404():
    r = client.delete("/admin/keys/99999", headers=_admin_headers())
    assert r.status_code == 404
