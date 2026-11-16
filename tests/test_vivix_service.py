"""Durable mock-only workflow: crashes, concurrent workers and money never retry a POST."""
import asyncio
import time
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from aibroker.db.engine import get_session
from aibroker.db.vivix_models import VivixLedgerLock
from aibroker.providers.gemini_video import VideoRejected
from aibroker.providers.vivix import VivixVideoTransport
from aibroker.providers.vivix_admission import VivixQuote, request_digest
from aibroker.services.vivix_service import VivixService
from aibroker.services.vivix_store import VivixLimits, VivixStore

MODEL = "vivix-w1"
INPUTS = {"jobs": [{"prompt": "fixture", "duration": 5}]}
LIMITS = VivixLimits("10", "10", "10")


def quote(**changes):
    return replace(VivixQuote("account", request_digest(MODEL, INPUTS), "2.25",
                              "synthetic-fixture-not-a-tariff", time.time() + 600, True), **changes)


async def prepare(service, job_id="job", **changes):
    args = {"job_id": job_id, "project_id": "project", "account_id": "account", "model": MODEL,
            "inputs": INPUTS, "allowed_models": frozenset({MODEL}), "quote": quote(), "limits": LIMITS}
    args.update(changes)
    return await service.prepare(**args)


async def advance(service, **changes):
    args = {"job_id": "job", "project_id": "project", "account_id": "account", "api_key": "FAKE"}
    args.update(changes)
    return await service.advance(**args)


@pytest.fixture
async def ledger(db):
    async with get_session() as session:
        session.add(VivixLedgerLock(id=1, version=0))
    return VivixStore()


def transport(calls, *, post="ok", status="succeeded"):
    def handler(req):
        calls.append(req.method)
        if req.url.path == "/v1/models":
            data = {"models": [{"name": MODEL}]}
        elif req.method == "POST":
            if post == "timeout":
                raise httpx.ReadTimeout("secret-provider-text")
            if post == "malformed":
                return httpx.Response(200, text="secret-provider-text")
            if post == "500":
                return httpx.Response(500)
            data = {"results": [{"job_id": "remote1", "status": "queued"}]}
        else:
            data = {"job_id": "remote1", "status": status,
                    "video_url": "https://example.test/fixture.mp4"}
        return httpx.Response(200, json={"code": 0, "data": data})
    return VivixVideoTransport(http_transport=httpx.MockTransport(handler),
                               enabled_models=frozenset({MODEL}))


@pytest.mark.parametrize("changes", [
    {"quote": None}, {"quote": quote(ceiling_usd="0")},
    {"quote": quote(ceiling_usd="NaN")}, {"quote": quote(ceiling_usd="Infinity")},
    {"quote": quote(ceiling_usd="-1")}, {"quote": quote(expires_at=0)},
    {"quote": quote(expires_at=float("inf"))},
    {"quote": quote(account_id="other")}, {"quote": quote(evidence="")},
    {"quote": quote(includes_reference_fees=False)},
    {"quote": quote(request_hash="other")}, {"allowed_models": frozenset()},
    {"quote": quote(ceiling_usd="0.000000001")},
])
async def test_fail_closed_before_http_or_reservation(ledger, changes):
    calls = []
    service = VivixService(ledger, transport(calls))
    with pytest.raises(VideoRejected):
        await prepare(service, **changes)
    assert calls == []
    with pytest.raises(VideoRejected, match="not found"):
        await ledger.load("job", "project")


async def test_restart_poll_and_explicit_settlement(ledger):
    calls = []
    service = VivixService(ledger, transport(calls))
    await prepare(service)
    row = await advance(service)
    assert row.body["state"] == "submitted"
    service = VivixService(VivixStore(), transport(calls))
    row = await advance(service)
    assert row.body["state"] == "terminal"
    assert Decimal(row.reserved_usd) == Decimal("2.25")
    assert Decimal(row.settled_usd) == 0  # terminal status is NOT evidence of zero billing
    row = await service.reconcile(job_id="job", project_id="project", actual_usd="1.50",
                                  evidence="fixture-receipt")
    assert row.body["state"] == "settled" and Decimal(row.reserved_usd) == 0
    assert Decimal(row.settled_usd) == Decimal("1.50")
    again = await service.reconcile(job_id="job", project_id="project", actual_usd="1.50",
                                    evidence="fixture-receipt")
    assert again.version == row.version
    await advance(service)
    assert calls == ["GET", "POST", "GET"]


@pytest.mark.parametrize("post", ["timeout", "malformed", "500"])
async def test_ambiguous_submit_retains_money_and_never_repeats(ledger, post):
    calls = []
    service = VivixService(ledger, transport(calls, post=post))
    await prepare(service)
    row = await advance(service)
    assert row.body["state"] == "unknown"
    assert "secret" not in str(row.body)
    for _ in range(3):
        await advance(VivixService(VivixStore(), transport(calls)))
    assert calls == ["GET", "POST"]
    assert Decimal(row.reserved_usd) == Decimal("2.25")


async def test_crash_after_post_before_task_save_never_resubmits(ledger, monkeypatch):
    calls = []
    service = VivixService(ledger, transport(calls))
    await prepare(service)
    real_save = ledger.save

    async def failing_save(old, body, **kw):
        if body["state"] == "submitted":
            raise OSError("DB unavailable")
        return await real_save(old, body, **kw)

    monkeypatch.setattr(ledger, "save", failing_save)
    with pytest.raises(OSError):
        await advance(service)
    row = await advance(VivixService(VivixStore(), transport(calls)))
    assert row.body["state"] == "submitting"
    assert Decimal(row.reserved_usd) == Decimal("2.25")
    assert calls == ["GET", "POST"]


async def test_concurrent_workers_only_one_post(ledger):
    calls = []
    service = VivixService(ledger, transport(calls))
    await prepare(service)
    await asyncio.gather(advance(service), advance(service))
    assert calls.count("POST") == 1


async def test_concurrent_admissions_cannot_overspend(ledger):
    service = VivixService(ledger, transport([]))
    results = await asyncio.gather(
        prepare(service, "a", limits=VivixLimits("3", "3", "3")),
        prepare(service, "b", limits=VivixLimits("3", "3", "3")), return_exceptions=True)
    assert sum(isinstance(r, VideoRejected) for r in results) == 1


@pytest.mark.parametrize("limits", [VivixLimits("2", "10", "10"),
                                   VivixLimits("10", "2", "10"),
                                   VivixLimits("10", "10", "2")])
async def test_all_three_caps_apply(ledger, limits):
    with pytest.raises(VideoRejected, match="budget"):
        await prepare(VivixService(ledger, transport([])), limits=limits)


async def test_cancel_before_submit_releases_reserve(ledger):
    calls = []
    service = VivixService(ledger, transport(calls))
    await prepare(service)
    row = await service.cancel(job_id="job", project_id="project")
    assert row.body["state"] == "cancelled" and Decimal(row.reserved_usd) == 0
    await advance(service)
    assert calls == []


async def test_cancel_after_submit_keeps_polling_and_hold(ledger):
    service = VivixService(ledger, transport([]))
    await prepare(service)
    await advance(service)
    row = await service.cancel(job_id="job", project_id="project")
    assert row.body["cancel_requested"] and Decimal(row.reserved_usd) > 0
    row = await advance(service)
    assert row.body["state"] == "terminal" and Decimal(row.reserved_usd) > 0


async def test_account_and_project_binding(ledger):
    calls = []
    service = VivixService(ledger, transport(calls))
    await prepare(service)
    with pytest.raises(VideoRejected):
        await advance(service, account_id="other")
    with pytest.raises(VideoRejected):
        await advance(service, project_id="other")
    assert calls == []


async def test_request_idempotency_and_conflict(ledger):
    service = VivixService(ledger, transport([]))
    first = await prepare(service)
    assert (await prepare(service)).version == first.version
    other = {"jobs": [{"prompt": "different"}]}
    with pytest.raises(VideoRejected, match="conflict"):
        await prepare(service, inputs=other, quote=quote(request_hash=request_digest(MODEL, other)))


async def test_poll_failure_can_retry_get_not_post(ledger):
    calls = []
    service = VivixService(ledger, transport(calls, status="unexpected"))
    await prepare(service)
    await advance(service)
    assert (await advance(service)).body["state"] == "submitted"
    assert (await advance(service)).body["state"] == "submitted"
    assert calls == ["GET", "POST", "GET", "GET"]


async def test_actual_overrun_blocks_future_admission(ledger):
    service = VivixService(ledger, transport([]))
    await prepare(service)
    await advance(service)
    await advance(service)
    row = await service.reconcile(job_id="job", project_id="project", actual_usd="11",
                                  evidence="fixture-receipt")
    assert row.body["over_ceiling"] and Decimal(row.settled_usd) == 11
    with pytest.raises(VideoRejected, match="budget"):
        await prepare(service, "second")
    with pytest.raises(VideoRejected, match="conflict"):
        await service.reconcile(job_id="job", project_id="project", actual_usd="0",
                                evidence="fixture-receipt")


async def test_expired_quote_before_submit_refunds_without_http(ledger, monkeypatch):
    calls = []
    service = VivixService(ledger, transport(calls))
    row = await prepare(service)
    monkeypatch.setattr("aibroker.services.vivix_service.time.time", lambda: row.body["quote_expires"])
    row = await advance(service)
    assert row.body["state"] == "expired" and Decimal(row.reserved_usd) == 0
    assert calls == []
