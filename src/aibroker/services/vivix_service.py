"""One durable Vivix transition per call; deliberately not registered as a public route."""
from __future__ import annotations

import copy
import time
from typing import Any

from aibroker.providers.gemini_video import VideoRejected
from aibroker.providers.vivix import VivixVideoTransport
from aibroker.providers.vivix_admission import VivixQuote, admit, request_digest, usd
from aibroker.services.vivix_store import Snapshot, VivixLimits, VivixStore, money


class VivixService:
    def __init__(self, store: VivixStore, transport: VivixVideoTransport) -> None:
        self.store = store
        self.transport = transport

    async def prepare(self, *, job_id: str, project_id: str, account_id: str,
                      model: str, inputs: dict[str, Any], allowed_models: frozenset[str],
                      quote: VivixQuote | None, limits: VivixLimits) -> Snapshot:
        inputs = copy.deepcopy(inputs)
        reserve = admit(model=model, inputs=inputs, account_id=account_id,
                        allowed_models=allowed_models, quote=quote, now=time.time())
        assert quote is not None
        return await self.store.create(
            job_id=job_id, project_id=project_id, account_id=account_id, reserve=reserve,
            limits=limits, body={"state": "prepared", "model": model, "inputs": inputs,
                                 "request_hash": request_digest(model, inputs),
                                 "quote_evidence": quote.evidence, "quote_expires": quote.expires_at,
                                 "ceiling_usd": reserve, "cancel_requested": False})

    async def advance(self, *, job_id: str, project_id: str, account_id: str,
                      api_key: str) -> Snapshot:
        old = await self.store.load(job_id, project_id)
        if old.account_id != account_id:
            raise VideoRejected("Vivix job is bound to another account")
        body = dict(old.body)
        state = body["state"]
        if state == "prepared":
            if time.time() >= body["quote_expires"]:
                await self.store.save(old, {**body, "state": "expired"}, reserved="0")
                return await self.store.load(job_id, project_id)
            # A crash/cancellation after this commit MUST NOT make the POST retryable.
            if not await self.store.save(old, {**body, "state": "submitting"}):
                return await self.store.load(job_id, project_id)
            old = await self.store.load(job_id, project_id)
            body = dict(old.body)
            try:
                result = await self.transport.submit_video(
                    model=body["model"], inputs=body["inputs"], api_key=api_key)
            except Exception:
                # Includes malformed envelopes and HTTP errors: no assumed non-billing semantics.
                await self.store.save(old, {**body, "state": "unknown"})
                return await self.store.load(job_id, project_id)
            await self.store.save(old, {**body, "state": "submitted", "task_id": result.id})
        elif state == "submitted":
            try:
                result = await self.transport.poll_video(
                    model=body["model"], video_id=body["task_id"], api_key=api_key)
            except Exception:
                # Polling can be retried, never submission. Do not persist upstream text/keys.
                return old
            body.update(provider_status=result.status)
            if result.terminal:
                body.update(state="terminal", url=result.url)
            await self.store.save(old, body)
        return await self.store.load(job_id, project_id)

    async def cancel(self, *, job_id: str, project_id: str) -> Snapshot:
        old = await self.store.load(job_id, project_id)
        body = dict(old.body)
        if body["state"] == "prepared":
            await self.store.save(old, {**body, "state": "cancelled"}, reserved="0")
        elif body["state"] in {"submitted", "terminal", "unknown"}:
            await self.store.save(old, {**body, "cancel_requested": True})
        # Do not change a submitting version: its worker must be able to save the returned task ID.
        # The caller can retry cancellation once this attempt has returned, or reconcile a crash.
        return await self.store.load(job_id, project_id)

    async def reconcile(self, *, job_id: str, project_id: str, actual_usd: str,
                        evidence: str) -> Snapshot:
        """Internal trusted receipt, not a public user assertion or provider-status heuristic."""
        amount = money(actual_usd)
        if not evidence.strip():
            raise VideoRejected("Vivix billing evidence is required")
        old = await self.store.load(job_id, project_id)
        body = dict(old.body)
        if body["state"] == "settled":
            if usd(old.settled_usd) != amount or body["billing_evidence"] != evidence:
                raise VideoRejected("Vivix settlement conflict")
            return old
        if body["state"] not in {"terminal", "unknown"}:
            raise VideoRejected("Vivix result must be terminal or reconciled as unknown")
        # Record overruns honestly, even above the original ceiling; future admissions see the debt.
        body.update(state="settled", billing_evidence=evidence,
                    over_ceiling=amount > usd(body["ceiling_usd"]))
        if not await self.store.save(old, body, reserved="0", settled=str(amount)):
            raise VideoRejected("Vivix concurrent transition; reread before reconciliation")
        return await self.store.load(job_id, project_id)

    async def recover_submission(self, *, job_id: str, project_id: str,
                                 evidence: str, task_id: str | None = None) -> Snapshot:
        """Trusted operator recovery after worker loss or authoritative submission lookup."""
        if not evidence.strip() or (task_id is not None and (
                not task_id or task_id in {".", ".."})):
            raise VideoRejected("Vivix recovery evidence and valid task identity are required")
        old = await self.store.load(job_id, project_id)
        if old.body["state"] not in {"submitting", "unknown"}:
            raise VideoRejected("Vivix submission is not awaiting recovery")
        body = {**old.body, "recovery_evidence": evidence,
                "state": "submitted" if task_id else "unknown"}
        if task_id:
            body["task_id"] = task_id
        if not await self.store.save(old, body):
            raise VideoRejected("Vivix concurrent transition; reread before recovery")
        return await self.store.load(job_id, project_id)
