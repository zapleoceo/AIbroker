"""ORM models — mirror infra/sql/init.sql."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
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
from sqlalchemy.dialects.postgresql import JSONB as _PG_JSONB

# JSONB on Postgres, plain JSON on SQLite (for tests). Same Python API.
JSONB = JSON().with_variant(_PG_JSONB(), "postgresql")
from sqlalchemy.orm import Mapped, mapped_column

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
    # Reserved-lane: picked LAST within its (provider, scope) group. A key scoped
    # only to llm:edit + is_reserve=True is the Coach safety net — used only when
    # all shared edit keys are exhausted, and invisible to bot llm:chat traffic.
    is_reserve: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
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
    # Discovered free-tier limits — populated from response headers at
    # first probe (admin/dashboard key-create flows). NULL ⇒ fall back to
    # PROVIDER_QUOTAS defaults in providers/quotas.py.
    discovered_req_limit: Mapped[int | None] = mapped_column(BigInteger)
    discovered_tok_limit: Mapped[int | None] = mapped_column(BigInteger)
    limits_discovered_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Manual per-key quota override — highest priority. Set when the operator
    # knows the real cap (e.g. a corporate Gemini key: 3M in / 80k out). NULL
    # on any axis ⇒ defer to discovered_*, then PROVIDER_QUOTAS default.
    manual_req_limit: Mapped[int | None] = mapped_column(BigInteger)
    manual_tok_limit: Mapped[int | None] = mapped_column(BigInteger)
    manual_tok_in_limit: Mapped[int | None] = mapped_column(BigInteger)
    manual_tok_out_limit: Mapped[int | None] = mapped_column(BigInteger)
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


class ProviderObservationRow(Base):
    """Self-learned, provider-level facts from real responses — so hardcoded
    constants are only seeds, overridden once reality is observed."""
    __tablename__ = "provider_observations"

    provider: Mapped[str] = mapped_column(String(50), primary_key=True)
    learned_max_request_tokens: Mapped[int | None] = mapped_column(BigInteger)
    learned_at: Mapped[datetime | None] = mapped_column(DateTime)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


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
