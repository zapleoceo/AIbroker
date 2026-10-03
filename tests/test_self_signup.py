"""Self-signup (POST /v1/signup), the lifetime request cap, and their admin UI.

Runs on SQLite (the PK is an INTEGER variant there) except the atomic-counter
race, which needs Postgres row locking.
"""
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from aibroker.auth import generate_project_key, hash_project_key
from aibroker.auth_session import COOKIE_NAME, issue_session_cookie
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.main import app
from aibroker.routing import CostGuardError, reserve_cost
from aibroker.services.request_cap import RequestCapExhausted, admit_request
from aibroker.services.signup import SignupNameInvalid, slugify_name

client = TestClient(app)
_DB = os.environ.get("DATABASE_URL", "")
ON_POSTGRES = "postgres" in _DB or "asyncpg" in _DB


def _ip(n: int = 1) -> dict[str, str]:
    return {"X-Forwarded-For": f"203.0.113.{n}, 10.0.0.1"}


def _signup(name: str = "My Bot", ip: int = 1, **extra):
    return client.post("/v1/signup", json={"name": name, **extra}, headers=_ip(ip))


async def _project(name: str = "p", **kw) -> tuple[str, int]:
    plain = generate_project_key()
    async with get_session() as s:
        row = ProjectRow(name=name, project_key_hash=hash_project_key(plain),
                         project_key_prefix=plain[:12],
                         allowed_scopes=kw.pop("scopes", ["llm:embed"]), **kw)
        s.add(row)
        await s.flush()
        return plain, row.id


async def _row(pid: int) -> ProjectRow:
    async with get_session() as s:
        return await s.get(ProjectRow, pid)


def _owner() -> dict[str, str]:
    cookie, _ = issue_session_cookie(get_settings().OWNER_TELEGRAM_ID or 169510539)
    return {COOKIE_NAME: cookie}


# ─── name normalisation ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,slug", [
    ("My Bot!", "my-bot"), ("  Stepan_AI  ", "stepan_ai"), ("123 bot", "bot"),
    ("Бот cool-svc", "cool-svc"), ("a" * 200, "a" * 60),
])
def test_slugify_name(raw, slug):
    assert slugify_name(raw) == slug


@pytest.mark.parametrize("raw", ["", "!", "1", "Бот", "---"])
def test_slugify_rejects_unusable(raw):
    with pytest.raises(SignupNameInvalid):
        slugify_name(raw)


# ─── signup endpoint ────────────────────────────────────────────────────────


async def test_signup_happy_path_creates_restricted_project():
    r = _signup("My Bot", contact="dev@example.com", purpose="testing")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "my-bot"
    assert body["project_key"].startswith("aib_prj_")
    assert body["limits"] == {"daily_cost_cap_usd": 0.0, "total_request_cap": 100,
                              "scopes": ["llm:chat", "llm:embed"]}
    assert body["docs_url"].endswith("/docs")
    async with get_session() as s:
        row = (await s.execute(select(ProjectRow))).scalar_one()
    assert row.self_signup is True and row.daily_cost_cap_usd == 0.0
    assert row.total_request_cap == 100 and row.total_requests_used == 0
    assert row.owner_email == "dev@example.com"
    assert row.signup_ip == "203.0.113.1"            # first X-Forwarded-For entry
    assert "testing" in row.notes and "203.0.113.1" in row.notes
    # the plaintext key is NOT stored, and it authenticates
    assert row.project_key_hash == hash_project_key(body["project_key"])
    assert client.get("/v1/models", headers={"X-Project-Key": body["project_key"]}).status_code == 200


async def test_signup_is_audited_and_notifies_owner():
    with patch("aibroker.services.signup.alert", AsyncMock()) as notify,          patch("aibroker.services.signup.audit", AsyncMock()) as aud:   # audit_log has no SQLite autoincrement
        assert _signup("audited", contact="a@b.c").status_code == 200
    notify.assert_awaited_once()
    assert notify.await_args.args[0] == "self_signup"
    aud.assert_awaited_once()
    assert aud.await_args.kwargs["action"] == "project.self_signup"
    assert aud.await_args.kwargs["target"] == "audited"
    assert aud.await_args.kwargs["ip"] == "203.0.113.1"


async def test_notifier_failure_does_not_fail_signup():
    with patch("aibroker.services.signup.alert", AsyncMock(side_effect=RuntimeError("tg down"))):
        assert _signup("resilient").status_code == 200


async def test_signup_name_clash_gets_a_suffix():
    first = _signup("Same Name", ip=1).json()["name"]
    second = _signup("same name", ip=2).json()["name"]
    assert first == "same-name"
    assert second != first and second.startswith("same-name-")


async def test_signup_invalid_name_is_422():
    r = _signup("!!")
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_name"


async def test_signup_honours_default_scopes_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_DEFAULT_SCOPES", "llm:embed, ")
    assert _signup("scoped").json()["limits"]["scopes"] == ["llm:embed"]


async def test_per_ip_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_PER_IP_PER_DAY", 2)
    assert _signup("one", ip=7).status_code == 200
    assert _signup("two", ip=7).status_code == 200
    r = _signup("three", ip=7)
    assert r.status_code == 429
    assert r.json()["error"] == "signup_rate_limited" and r.json()["scope"] == "ip"
    assert r.headers["retry-after"]
    assert _signup("other-ip", ip=8).status_code == 200      # another address is fine


async def test_per_ip_limit_ignores_old_signups(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_PER_IP_PER_DAY", 1)
    assert _signup("old", ip=9).status_code == 200
    async with get_session() as s:
        await s.execute(text("UPDATE projects SET created_at = :t"),
                        {"t": datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)})
    assert _signup("fresh", ip=9).status_code == 200


async def test_global_daily_limit(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_PER_DAY", 2)
    assert _signup("g1", ip=1).status_code == 200
    assert _signup("g2", ip=2).status_code == 200
    r = _signup("g3", ip=3)
    assert r.status_code == 429 and r.json()["scope"] == "global"


async def test_manual_projects_do_not_count_against_signup_limits(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_PER_DAY", 1)
    await _project("hand-made")
    assert _signup("first-signup").status_code == 200


async def test_kill_switch(monkeypatch):
    monkeypatch.setattr(get_settings(), "SIGNUP_ENABLED", False)
    r = _signup("nope")
    assert r.status_code == 403 and r.json()["error"] == "signup_disabled"
    async with get_session() as s:
        assert (await s.execute(select(ProjectRow))).first() is None


def test_signup_needs_no_auth_and_is_in_openapi():
    assert "/v1/signup" in client.get("/openapi.json").json()["paths"]


# ─── cap 0 = free providers only (NOT unlimited) ────────────────────────────


def _fake_key(tier: str) -> ApiKeyRow:
    return ApiKeyRow(id=999_999, provider="x", label="x", tier=tier,
                     token_encrypted="x", scopes=["llm:chat"], daily_cost_cap_usd=None)


async def test_zero_daily_cap_admits_free_and_blocks_paid():
    project = ProjectRow(id=5, name="z", project_key_hash="x", project_key_prefix="x",
                         allowed_scopes=["llm:chat"], daily_cost_cap_usd=0.0)
    await reserve_cost(api_key=_fake_key("free"), project=project, estimated_cost=0.0)
    with pytest.raises(CostGuardError) as e:
        await reserve_cost(api_key=_fake_key("paid"), project=project, estimated_cost=0.001)
    assert e.value.kind == "project" and e.value.limit == 0.0


# ─── request cap: admission + 429 shape ─────────────────────────────────────


async def test_uncapped_project_never_writes():
    _, pid = await _project("legacy")
    proj = await _row(pid)
    assert proj.total_request_cap is None and proj.total_requests_used == 0
    for _ in range(3):
        await admit_request(proj)
    assert (await _row(pid)).total_requests_used == 0     # NULL cap: counter untouched


async def test_cap_counts_then_refuses():
    _, pid = await _project("capped", total_request_cap=2)
    proj = await _row(pid)
    await admit_request(proj)
    await admit_request(proj)
    with pytest.raises(RequestCapExhausted) as e:
        await admit_request(proj)
    assert (e.value.limit, e.value.used) == (2, 2)
    assert (await _row(pid)).total_requests_used == 2     # the refused one is not counted


async def test_stale_project_object_sees_the_live_cap():
    _, pid = await _project("live", total_request_cap=1)
    stale = await _row(pid)
    async with get_session() as s:
        await s.execute(text("UPDATE projects SET total_request_cap = 3 WHERE id = :i"), {"i": pid})
    for _ in range(3):
        await admit_request(stale)
    with pytest.raises(RequestCapExhausted):
        await admit_request(stale)


async def test_cap_lifted_mid_flight_admits():
    _, pid = await _project("lifted", total_request_cap=1)
    stale = await _row(pid)
    async with get_session() as s:
        await s.execute(text("UPDATE projects SET total_request_cap = NULL WHERE id = :i"),
                        {"i": pid})
    await admit_request(stale)
    await admit_request(stale)                             # no raise


async def test_exhausted_cap_is_429_with_stable_json_on_every_billable_endpoint():
    plain, pid = await _project("full", total_request_cap=1,
                                scopes=["llm:chat", "llm:embed", "llm:decision",
                                        "llm:audio", "llm:deep"])
    async with get_session() as s:
        await s.execute(text("UPDATE projects SET total_requests_used = 1 WHERE id = :i"),
                        {"i": pid})
    h = {"X-Project-Key": plain}
    audio = {"file": ("a.ogg", b"OggS" + b"\0" * 64, "audio/ogg")}
    calls = [
        client.post("/v1/embed", headers=h, json={"input": ["x"]}),
        client.post("/v1/jobs?capability=chat:fast", headers=h,
                    json={"messages": [{"role": "user", "content": "hi"}]}),
        client.post("/v1/deep", headers=h,
                    json={"messages": [{"role": "user", "content": "hi"}]}),
        client.post("/v1/decisions", headers=h, json={
            "state": "s", "questions": {"q": {"type": "noul", "instructions": "ok?",
                                              "criteria": {"true": "yes", "false": "no"}}}}),
        client.post("/v1/transcribe", headers=h, files=audio),
        client.post("/v1/transcribe/jobs", headers=h, files=audio),
    ]
    for r in calls:
        assert r.status_code == 429, r.text
    for r in calls:
        j = r.json()
        assert j["error"] == "request_cap_exhausted"
        assert j["limit"] == 1 and j["used"] == 1 and "message" in j
        assert "detail" not in j


async def test_admitted_request_is_counted_once_per_client_request():
    plain, pid = await _project("once", total_request_cap=5)
    fake = AsyncMock(return_value=None)                    # provider pool empty -> 503
    with patch("aibroker.routes.proxy.run_embed", fake):
        r = client.post("/v1/embed", headers={"X-Project-Key": plain}, json={"input": ["x"]})
    assert r.status_code == 503
    assert (await _row(pid)).total_requests_used == 1


async def test_rejected_request_does_not_spend_the_allowance():
    plain, pid = await _project("noscope", total_request_cap=5, scopes=["llm:chat"])
    r = client.post("/v1/embed", headers={"X-Project-Key": plain}, json={"input": ["x"]})
    assert r.status_code == 403
    assert (await _row(pid)).total_requests_used == 0


@pytest.mark.skipif(not ON_POSTGRES, reason="needs Postgres row locking")
async def test_concurrent_admissions_never_overshoot_the_cap():
    _, pid = await _project("race", total_request_cap=7)
    proj = await _row(pid)

    async def one() -> bool:
        try:
            await admit_request(proj)
            return True
        except RequestCapExhausted:
            return False

    results = await asyncio.gather(*(one() for _ in range(40)))
    assert sum(results) == 7
    assert (await _row(pid)).total_requests_used == 7


# ─── admin UI ───────────────────────────────────────────────────────────────


async def test_dashboard_edit_sets_and_clears_request_cap():
    _, pid = await _project("ui", total_request_cap=100, daily_cost_cap_usd=0.0)
    base = {"name": "ui", "allowed_scopes": "llm:chat", "daily_cost_cap_usd": "0"}
    r = client.post(f"/dashboard/projects/{pid}/edit", cookies=_owner(), follow_redirects=False,
                    data={**base, "total_request_cap": "250", "req_cap_present": "1"})
    assert r.status_code == 303
    row = await _row(pid)
    assert row.total_request_cap == 250 and row.daily_cost_cap_usd == 0.0
    client.post(f"/dashboard/projects/{pid}/edit", cookies=_owner(), follow_redirects=False,
                data={**base, "total_request_cap": "", "req_cap_present": "1"})
    assert (await _row(pid)).total_request_cap is None     # blank = unlimited


async def test_dashboard_edit_without_field_keeps_the_cap():
    _, pid = await _project("keep", total_request_cap=100)
    client.post(f"/dashboard/projects/{pid}/edit", cookies=_owner(), follow_redirects=False,
                data={"name": "keep", "allowed_scopes": "llm:chat", "daily_cost_cap_usd": "0"})
    assert (await _row(pid)).total_request_cap == 100


@pytest.mark.parametrize("bad", ["-1", "abc", "1.5"])
async def test_dashboard_edit_rejects_bad_request_cap(bad):
    _, pid = await _project("bad", total_request_cap=100)
    r = client.post(f"/dashboard/projects/{pid}/edit", cookies=_owner(), follow_redirects=False,
                    data={"name": "bad", "allowed_scopes": "llm:chat",
                          "daily_cost_cap_usd": "0", "total_request_cap": bad,
                          "req_cap_present": "1"})
    assert "flash=%21Bad+request+cap" in r.headers["location"]
    assert (await _row(pid)).total_request_cap == 100


async def test_dashboard_create_accepts_request_cap():
    r = client.post("/dashboard/projects/create", cookies=_owner(), follow_redirects=False,
                    data={"name": "made", "allowed_scopes": "llm:chat",
                          "daily_cost_cap_usd": "0", "total_request_cap": "50"})
    assert r.status_code == 303
    async with get_session() as s:
        row = (await s.execute(select(ProjectRow).where(ProjectRow.name == "made"))).scalar_one()
    assert row.total_request_cap == 50 and row.self_signup is False


async def test_projects_page_badge_filter_and_progress():
    assert _signup("badge-me").status_code == 200
    await _project("manual-one")
    page = client.get("/dashboard/projects", cookies=_owner()).text
    assert "self-signup" in page and "badge-me" in page and "manual-one" in page
    assert "0 / 100" in page                               # request-cap meter
    only = client.get("/dashboard/projects?signup=1", cookies=_owner()).text
    assert "badge-me" in only and "manual-one" not in only


async def test_project_settings_tab_shows_usage_and_field():
    _, pid = await _project("tab", total_request_cap=100, total_requests_used=40)
    page = client.get(f"/dashboard/projects/{pid}?tab=settings", cookies=_owner()).text
    assert "40 / 100" in page and 'name="total_request_cap"' in page and 'value="100"' in page
    _, free = await _project("tab-free")
    page = client.get(f"/dashboard/projects/{free}?tab=settings", cookies=_owner()).text
    assert "no cap" in page


def test_request_cap_view_extremes():
    from aibroker.routes.dashboard_views import request_cap_view
    mk = lambda cap, used: request_cap_view(  # noqa: E731
        type("P", (), {"total_request_cap": cap, "total_requests_used": used})())
    assert mk(None, 5)["pct"] is None
    assert mk(0, 0)["pct"] == 100
    assert mk(100, 250)["pct"] == 100
    assert mk(100, 50)["pct"] == 50


def test_landing_and_llms_txt_advertise_signup():
    assert "/v1/signup" in client.get("/").text
    assert "POST /v1/signup" in client.get("/llms.txt").text
