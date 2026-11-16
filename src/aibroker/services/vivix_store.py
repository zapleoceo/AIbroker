"""Transactional per-job ledger and conservative lifetime caps for staged Vivix jobs."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db.engine import get_session
from aibroker.db.vivix_models import VivixJobRow, VivixLedgerLock
from aibroker.providers.gemini_video import VideoRejected
from aibroker.providers.vivix_admission import usd


@dataclass(frozen=True)
class VivixLimits:
    """Explicit lifetime budgets for this isolated ledger; no implicit resets."""

    account_usd: str
    project_usd: str
    global_usd: str


@dataclass(frozen=True)
class Snapshot:
    id: str
    project_id: str
    account_id: str
    version: int
    reserved_usd: str
    settled_usd: str
    body: dict[str, Any]


def snapshot(row: VivixJobRow) -> Snapshot:
    return Snapshot(row.id, row.project_id, row.account_id, row.version,
                    str(row.reserved_usd), str(row.settled_usd), dict(row.body))


def money(value: str) -> Decimal:
    amount = usd(value)
    if amount >= Decimal("1000000000000000") or amount != amount.quantize(Decimal("0.00000001")):
        raise VideoRejected("USD amount exceeds ledger precision")
    return amount


class VivixStore:
    async def before_create(self, session: AsyncSession, project_id: str, account_id: str,
                            body: dict[str, Any], amount: Decimal) -> None:
        if body.get("broker_key_id") is not None:
            raise VideoRejected("Broker-bound Vivix jobs require BrokerVivixStore")

    async def after_settle(self, session: AsyncSession, old: Snapshot,
                           body: dict[str, Any], amount: Decimal) -> None:
        if old.body.get("broker_key_id") is not None:
            raise VideoRejected("Broker-bound Vivix settlement requires BrokerVivixStore")

    async def load(self, job_id: str, project_id: str) -> Snapshot:
        async with get_session() as session:
            row = await session.get(VivixJobRow, job_id)
            if row is None or row.project_id != project_id:
                raise VideoRejected("Vivix job not found")
            return snapshot(row)

    async def create(self, *, job_id: str, project_id: str, account_id: str,
                     body: dict[str, Any], reserve: str, limits: VivixLimits) -> Snapshot:
        amount = money(reserve)
        if amount <= 0 or not all((job_id, project_id, account_id)):
            raise VideoRejected("Vivix reservation requires identities and a positive ceiling")
        caps = [money(limits.account_usd), money(limits.project_usd), money(limits.global_usd)]
        async with get_session() as session:
            # Every admission takes this DB lock; a SUM without it can over-admit concurrent jobs.
            lock = await session.execute(update(VivixLedgerLock).where(VivixLedgerLock.id == 1)
                                         .values(version=VivixLedgerLock.version + 1))
            if lock.rowcount != 1:
                raise VideoRejected("Vivix ledger migration is required")
            existing = await session.get(VivixJobRow, job_id)
            if existing is not None:
                if (existing.project_id != project_id or existing.account_id != account_id
                        or existing.body["request_hash"] != body["request_hash"]):
                    raise VideoRejected("Vivix job identity conflict")
                return snapshot(existing)
            filters = [VivixJobRow.account_id == account_id,
                       VivixJobRow.project_id == project_id, True]
            for predicate, cap in zip(filters, caps, strict=True):
                used = await session.scalar(select(func.coalesce(func.sum(
                    VivixJobRow.reserved_usd + VivixJobRow.settled_usd), 0)).where(predicate))
                if used + amount > cap:
                    raise VideoRejected("Vivix ledger budget exhausted")
            await self.before_create(session, project_id, account_id, body, amount)
            row = VivixJobRow(id=job_id, project_id=project_id, account_id=account_id,
                             api_key_id=body.get("broker_key_id"), version=0,
                             reserved_usd=amount, settled_usd=Decimal(0), body=body)
            session.add(row)
            await session.flush()
            return snapshot(row)

    async def save(self, old: Snapshot, body: dict[str, Any], *,
                   reserved: str | None = None, settled: str | None = None) -> bool:
        values: dict[str, Any] = {"body": body, "version": old.version + 1}
        if reserved is not None:
            values["reserved_usd"] = money(reserved)
        if settled is not None:
            values["settled_usd"] = money(settled)
        async with get_session() as session:
            if reserved is not None or settled is not None:
                await session.execute(update(VivixLedgerLock).where(VivixLedgerLock.id == 1)
                                      .values(version=VivixLedgerLock.version + 1))
            result = await session.execute(update(VivixJobRow).where(
                VivixJobRow.id == old.id, VivixJobRow.project_id == old.project_id,
                VivixJobRow.version == old.version).values(**values))
            if result.rowcount == 1 and settled is not None:
                await self.after_settle(session, old, body, money(settled))
            return result.rowcount == 1
