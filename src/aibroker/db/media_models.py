"""Submission tombstones survive job/artifact retention; migration 031."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from aibroker.db.engine import Base


class MediaDispatchRow(Base):
    __tablename__ = "media_dispatch"
    job_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    claim: Mapped[datetime | None] = mapped_column(DateTime)
    key_id: Mapped[int | None] = mapped_column(BigInteger)
    account_id: Mapped[str | None] = mapped_column(String(255))
    key_fingerprint: Mapped[str | None] = mapped_column(String(64))
    reservation_id: Mapped[str | None] = mapped_column(String(36))
    model: Mapped[str | None] = mapped_column(String(200))
    provider_id: Mapped[str | None] = mapped_column(String(255))
    usage_id: Mapped[int | None] = mapped_column(BigInteger)
