"""routes/health — public endpoints."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from aibroker.main import app


client = TestClient(app)

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


def test_landing_returns_html():
    r = client.get("/")
    assert r.status_code == 200
    assert "AIbroker" in r.text
    assert "/docs" in r.text


def test_healthz_returns_json():
    r = client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["service"] == "aibroker"
    assert "ts" in data


@pytest.mark.skipif(
    ON_SQLITE,
    reason="/v1/health uses Postgres-only now() and FILTER (...)",
)
def test_v1_health_returns_providers_array():
    r = client.get("/v1/health")
    assert r.status_code == 200
    data = r.json()
    assert "providers" in data
    assert isinstance(data["providers"], list)


def test_openapi_docs_served():
    r = client.get("/docs")
    assert r.status_code == 200
    assert "swagger" in r.text.lower() or "openapi" in r.text.lower()


def test_openapi_json_served():
    r = client.get("/openapi.json")
    assert r.status_code == 200
    spec = r.json()
    assert "paths" in spec
    assert "/healthz" in spec["paths"]
    assert "/v1/health" in spec["paths"]
    assert "/v1/chat" in spec["paths"]


def test_unknown_path_returns_404():
    r = client.get("/this/path/does/not/exist")
    assert r.status_code == 404
