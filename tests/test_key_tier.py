"""Per-request key tier (`key_tier`: auto | paid | free), docs/api.md "Key tier"."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow, UsageLogRow
from aibroker.main import app
from aibroker.routing.key_tier import normalize, paid_lane
from aibroker.routing.selector import record_usage
from aibroker.services import job_queue
from tests.test_routes_proxy import _make_project
from tests.test_selector import _add_key

client = TestClient(app)
_MSGS = [{"role": "user", "content": "hi"}]


def test_normalize_defaults_to_auto():
    assert normalize("paid") == "paid" and normalize("free") == "free"
    assert normalize(None) == "auto" and normalize("gold") == "auto" and normalize(3) == "auto"


def test_paid_lane_rejects_free_only():
    assert paid_lane("auto", "image") == "auto" and paid_lane("paid", "image") == "paid"
    with pytest.raises(ValueError, match="not available for image"):
        paid_lane("free", "image")


def _walk_env(monkeypatch, chain):
    """run_chat with every pick refused: records (provider, require_tier) per pick."""
    import aibroker.services.llm_service as svc

    picks: list[tuple[str, str | None]] = []

    async def fake_pick(provider, scope, **kw):
        picks.append((provider, kw.get("require_tier")))

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "chain_for", lambda cap: list(chain))
    return svc, picks


async def _walk(svc, **kw):
    return await svc.run_chat(
        project=SimpleNamespace(id=7, name="p"), capability="chat:smart",
        messages=_MSGS, model=None, max_tokens=64, temperature=0.7,
        response_format=None, workflow="w", **kw)


async def test_paid_walks_paid_keys_from_the_first_attempt(monkeypatch):
    svc, picks = _walk_env(monkeypatch, ["gemini", "deepseek"])
    assert await _walk(svc, key_tier="paid") is None
    assert picks and {tier for _, tier in picks} == {"paid"}


async def test_free_walks_free_keys_only_even_when_paid_only_is_asked(monkeypatch):
    svc, picks = _walk_env(monkeypatch, ["gemini", "deepseek"])
    assert await _walk(svc, key_tier="free", paid_only=True) is None
    assert picks and {tier for _, tier in picks} == {"free"}


async def test_auto_keeps_the_free_first_walk(monkeypatch):
    svc, picks = _walk_env(monkeypatch, ["gemini", "deepseek"])
    await _walk(svc)
    assert "paid" not in {tier for _, tier in picks}


async def test_free_drops_the_paid_pass_of_a_pinned_walk(monkeypatch):
    svc, picks = _walk_env(monkeypatch, ["gemini"])
    await svc.run_chat(
        project=SimpleNamespace(id=7, name="p"), capability="chat:smart",
        messages=_MSGS, model="gemini/gemini-2.5-flash", max_tokens=64, temperature=0.7,
        response_format=None, workflow="w", key_tier="free")
    assert picks and {tier for _, tier in picks} == {"free"}


def _row(pid: int, retry_count: int, key_tier: str | None) -> DeepJobRow:
    request = {"messages": _MSGS, "model": None, "max_tokens": 64, "temperature": 0.7,
               "response_format": None, "workflow": "t"}
    if key_tier:
        request["key_tier"] = key_tier
    return DeepJobRow(id=1, project_id=pid, capability="chat:smart", status="running",
                      retry_count=retry_count, request=request)


@pytest.mark.parametrize(("key_tier", "retry", "expect"), [
    (None, 0, ("auto", False)),
    ("paid", 0, ("paid", False)),          # paid from attempt 1 via run_chat(key_tier)
    ("free", job_queue._MAX_RETRIES - 1, ("free", False)),   # free never escalates
    (None, job_queue._MAX_RETRIES - 1, ("auto", True)),      # today's final-retry escalation
])
async def test_dispatcher_passes_key_tier(key_tier, retry, expect):
    pid = 990700 + retry * 10 + len(key_tier or "")
    async with get_session() as s:
        s.add(ProjectRow(id=pid, name=f"kt-{pid}", project_key_hash="h",
                         project_key_prefix="pk_x", allowed_scopes=["llm:chat"]))
    seen: list[tuple[str, bool]] = []

    async def fake_run_chat(**kw):
        seen.append((kw["key_tier"], kw["paid_only"]))

    with patch.object(job_queue, "run_chat", fake_run_chat), \
         patch.object(job_queue, "_requeue_or_fail", AsyncMock()) as requeue:
        await job_queue._execute(_row(pid, retry, key_tier))
    assert seen == [expect]
    reason = requeue.await_args.args[2]
    assert (f"key_tier={key_tier}" in reason) == (key_tier is not None)


async def test_submit_stores_key_tier_only_when_not_auto():
    plain, _ = await _make_project(["llm:chat"])
    ids = []
    for tier in ("paid", "auto"):
        r = client.post("/v1/jobs?capability=structured", headers={"X-Project-Key": plain},
                        json={"messages": _MSGS, "key_tier": tier,
                              "workflow": f"kt-{tier}"})
        assert r.status_code == 202, r.text
        ids.append(r.json()["job_id"])
    async with get_session() as s:
        rows = {r.id: r.request for r in (await s.scalars(
            select(DeepJobRow).where(DeepJobRow.id.in_(ids)))).all()}
    assert rows[ids[0]]["key_tier"] == "paid"
    assert "key_tier" not in rows[ids[1]]


async def test_submit_rejects_unknown_key_tier():
    plain, _ = await _make_project(["llm:chat"])
    r = client.post("/v1/jobs?capability=structured", headers={"X-Project-Key": plain},
                    json={"messages": _MSGS, "key_tier": "gold"})
    assert r.status_code == 422


async def test_paid_only_lanes_reject_free():
    plain, _ = await _make_project(["llm:image", "llm:video", "llm:speech"])
    h = {"X-Project-Key": plain}
    r = client.post("/v1/images/jobs", headers=h, json={"prompt": "cat", "key_tier": "free"})
    assert r.status_code == 422 and "not available for image" in r.text
    r = client.post("/v1/video/jobs", headers=h, json={"prompt": "cat", "key_tier": "free"})
    assert r.status_code == 422 and "not available for video" in r.text
    r = client.post("/v1/speech", headers=h, json={"text": "hi", "key_tier": "free"})
    assert r.status_code == 422 and "not available for speech" in r.text


async def test_usage_row_records_the_key_tier():
    kid = await _add_key("cerebras", "kt", tier="free")
    usage_id = await record_usage(
        api_key_id=kid, project_id=None, lease_id=None, provider="cerebras",
        model="gpt-oss-120b", capability="chat:fast", workflow="test", tokens_in=1,
        tokens_out=1, cost_usd=0.0, latency_ms=10, status="ok", error_kind=None,
        http_status=200)
    async with get_session() as s:
        row = await s.get(UsageLogRow, usage_id)
    assert row.key_tier == "free"


@pytest.mark.skipif("postgres" not in __import__("os").environ.get("DATABASE_URL", ""),
                    reason="pick_and_reserve SQL is Postgres-only (as tests/test_selector.py)")
async def test_free_tier_pick_includes_trial_keys():
    """NVIDIA's only key is tier "trial": a pinned walk's free pass must reach it."""
    from aibroker.routing.selector import pick_and_reserve

    kid = await _add_key("nvidia", "trial-key", tier="trial")
    key = await pick_and_reserve("nvidia", "llm:chat", require_tier="free")
    assert key is not None and key.id == kid
    assert await pick_and_reserve("nvidia", "llm:chat", require_tier="paid") is None
