"""main.py — app-wide middleware and schema exposure."""
from __future__ import annotations

from fastapi.testclient import TestClient

from aibroker.main import _security_headers, app

client = TestClient(app)

_EXPECTED = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "strict-transport-security": "max-age=31536000",
}


def test_security_headers_on_every_kind_of_response():
    """S15 (2026-10-02): none were set. HTML, JSON and an error page all get them."""
    for path in ("/", "/healthz", "/login", "/definitely-not-a-route"):
        r = client.get(path)
        for name, value in _EXPECTED.items():
            assert r.headers.get(name) == value, (path, name)


def test_csp_is_frame_ancestors_only():
    """No script-src/style-src: inline scripts and the Telegram login widget must keep working."""
    csp = client.get("/login").headers["content-security-policy"]
    assert csp == "frame-ancestors 'none'"
    assert "script-src" not in csp and "style-src" not in csp


def test_hsts_has_no_include_subdomains_or_preload():
    hsts = client.get("/healthz").headers["strict-transport-security"]
    assert "includeSubDomains" not in hsts and "preload" not in hsts


async def test_security_headers_do_not_override_a_route_header():
    from starlette.responses import Response

    async def call_next(_request):
        return Response("x", headers={"X-Frame-Options": "SAMEORIGIN"})

    resp = await _security_headers(None, call_next)
    assert resp.headers["x-frame-options"] == "SAMEORIGIN"
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_openapi_json_is_public_but_hides_owner_only_routes():
    """S17: it listed /dashboard/*, /admin/*, /login, /logout, /api/tg_login."""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/v1/jobs" in paths and "/v1/embed" in paths
    for hidden in ("/dashboard", "/admin", "/login", "/logout", "/api/tg_login"):
        assert not any(p.startswith(hidden) for p in paths), hidden
    assert "/dashboard" not in r.text and "/admin" not in r.text
