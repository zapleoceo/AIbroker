"""ORM models — mirror infra/sql/init.sql."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from aibroker.db.engine import Base


class ProjectRow(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    owner_email: Mapped[str | None] = mapped_column(String(255))
    project_key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    project_key_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    allowed_scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    daily_cost_cap_usd: Mapped[float | None] = mapped_column(Float)
    monthly_cost_cap_usd: Mapped[float | None] = mapped_column(Float)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ApiKeyRow(Base):
    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("provider", "label", name="uq_api_keys_provider_label"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    tier: Mapped[str] = mapped_column(String(10), default="free", nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_alive: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, default=999_999, nullable=False)
    daily_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    daily_cost_cap_usd: Mapped[float | None] = mapped_column(Float)
    daily_cost_used_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    monthly_cost_cap_usd: Mapped[float | None] = mapped_column(Float)
    monthly_cost_used_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    daily_reset_at: Mapped[date | None] = mapped_column(Date)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_alive_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class LeaseRow(Base):
    __tablename__ = "leases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    api_key_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    workflow: Mapped[str | None] = mapped_column(String(50))
    request_meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    leased_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime)


class UsageLogRow(Base):
    __tablename__ = "usage_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    api_key_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("api_keys.id", ondelete="SET NULL")
    )
    project_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="SET NULL")
    )
    lease_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("leases.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    capability: Mapped[str | None] = mapped_column(String(30))
    workflow: Mapped[str | None] = mapped_column(String(50))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_kind: Mapped[str | None] = mapped_column(String(80))
    http_status: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)


class AuditLogRow(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target: Mapped[str | None] = mapped_column(String(120))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, nullable=False
    )
    ip: Mapped[str | None] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
