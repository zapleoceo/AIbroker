"""Durable ordinary-attempt holds, shared with the Vivix admission ledger."""
from datetime import date
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Date, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from aibroker.db.engine import Base


class BudgetLedgerLock(Base):
    __tablename__ = "budget_ledger_lock"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class BudgetReservationRow(Base):
    __tablename__ = "budget_reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    api_key_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    month: Mapped[date] = mapped_column(Date, nullable=False)
    debit_daily: Mapped[bool] = mapped_column(Boolean, nullable=False)
    debit_monthly: Mapped[bool] = mapped_column(Boolean, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="held", index=True)
    usage_id: Mapped[int | None] = mapped_column(BigInteger)
    fingerprint: Mapped[str | None] = mapped_column(String(64))
