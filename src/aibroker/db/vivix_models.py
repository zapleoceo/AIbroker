"""Isolated Vivix ledger; not connected to the public dispatcher."""
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from aibroker.db.engine import Base


class VivixLedgerLock(Base):
    __tablename__ = "vivix_ledger_lock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class VivixJobRow(Base):
    __tablename__ = "vivix_jobs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    settled_usd: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    body: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
