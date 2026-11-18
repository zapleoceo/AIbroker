"""Recovery and cancellation across the durable Vivix submission boundary."""
import asyncio
from decimal import Decimal

import pytest

from aibroker.providers.gemini_video import VideoRejected
from aibroker.services.vivix_service import VivixService
from tests.test_vivix_service import advance, prepare, transport
from tests.test_vivix_service import ledger as _ledger_fixture

# Register shared fixtures without shadowing imported names in test arguments.
ledger = _ledger_fixture


async def test_crash_recovery_attaches_task_without_resubmitting(ledger):
    calls = []
    service = VivixService(ledger, transport(calls))
    old = await prepare(service)
    await ledger.save(old, {**old.body, "state": "submitting"})
    row = await service.recover_submission(job_id="job", project_id="project",
                                           task_id="remote1", evidence="fixture-lookup")
    assert row.body["state"] == "submitted" and Decimal(row.reserved_usd) > 0
    row = await advance(service)
    assert row.body["state"] == "terminal"
    assert calls == ["GET"]


async def test_unknown_recovery_needs_receipt_to_release_hold(ledger):
    service = VivixService(ledger, transport([]))
    old = await prepare(service)
    await ledger.save(old, {**old.body, "state": "submitting"})
    row = await service.recover_submission(job_id="job", project_id="project",
                                           evidence="fixture-worker-exit")
    assert row.body["state"] == "unknown" and Decimal(row.reserved_usd) > 0
    with pytest.raises(VideoRejected, match="evidence"):
        await service.reconcile(job_id="job", project_id="project", actual_usd="0", evidence="")
    row = await service.reconcile(job_id="job", project_id="project", actual_usd="0",
                                  evidence="fixture-confirmed-no-charge")
    assert Decimal(row.reserved_usd) == 0 and row.body["state"] == "settled"


async def test_cancellation_during_post_preserves_task_save(ledger):
    entered, proceed = asyncio.Event(), asyncio.Event()
    calls = []
    wire = transport(calls)
    real_submit = wire.submit_video

    async def blocked_submit(**kw):
        entered.set()
        await proceed.wait()
        return await real_submit(**kw)

    wire.submit_video = blocked_submit
    service = VivixService(ledger, wire)
    await prepare(service)
    worker = asyncio.create_task(advance(service))
    await entered.wait()
    row = await service.cancel(job_id="job", project_id="project")
    assert row.body["state"] == "submitting" and Decimal(row.reserved_usd) > 0
    proceed.set()
    row = await worker
    assert row.body["state"] == "submitted" and row.body["task_id"] == "remote1"
    assert calls == ["GET", "POST"]


async def test_task_cancellation_mid_post_stays_ambiguous(ledger):
    entered = asyncio.Event()

    async def cancelled_submit(**kw):
        entered.set()
        await asyncio.Future()

    wire = transport([])
    wire.submit_video = cancelled_submit
    service = VivixService(ledger, wire)
    await prepare(service)
    worker = asyncio.create_task(advance(service))
    await entered.wait()
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker
    row = await ledger.load("job", "project")
    assert row.body["state"] == "submitting" and Decimal(row.reserved_usd) > 0
