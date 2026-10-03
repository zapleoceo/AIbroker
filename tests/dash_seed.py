"""Shared seeding helpers for the admin-UI tests (portable: SQLite or Postgres).

Primary keys are explicit — SQLite cannot autoincrement a BIGINT PK.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient

from aibroker.auth_session import COOKIE_NAME, issue_session_cookie
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import (
    ApiKeyRow,
    AuditLogRow,
    DeepJobRow,
    ProjectRow,
    UsageLogRow,
)


def now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def cookies() -> dict[str, str]:
    uid = get_settings().OWNER_TELEGRAM_ID or 169510539
    cookie, _ = issue_session_cookie(uid)
    return {COOKIE_NAME: cookie}


def run(coro: Any) -> Any:
    return asyncio.get_event_loop().run_until_complete(coro)


def get(client: TestClient, path: str, **kw: Any):
    return client.get(path, cookies=cookies(), follow_redirects=False, **kw)


def project(id_: int = 1, name: str = "stepan", **kw: Any) -> ProjectRow:
    base: dict[str, Any] = {
        "id": id_, "name": name, "project_key_prefix": f"aib_prj_{id_:03d}", "project_key_hash": f"hash{id_}",
        "allowed_scopes": ["llm:chat", "llm:embed"], "is_active": True, "notes": ""}
    base.update(kw)
    return ProjectRow(**base)


def key(id_: int = 1, provider: str = "gemini", label: str = "k1", **kw: Any) -> ApiKeyRow:
    base: dict[str, Any] = {
        "id": id_, "provider": provider, "label": label, "tier": "free", "scopes": ["llm:chat"],
        "token_encrypted": "x", "is_active": True, "is_alive": True}
    base.update(kw)
    return ApiKeyRow(**base)


def usage(id_: int, *, minutes_ago: float = 5, project_id: int | None = 1,
          api_key_id: int | None = 1, provider: str = "gemini",
          model: str | None = "gemini/gemini-3.5-flash-lite", capability: str | None = "chat:fast",
          workflow: str | None = "triage", status: str = "ok", cost: float = 0.0,
          latency_ms: int | None = 800, tokens_in: int = 100, tokens_out: int = 20,
          cache_read: int = 0, error_kind: str | None = None, http_status: int | None = None,
          model_served: str | None = None, at: datetime | None = None,
          request_id: str | None = None) -> UsageLogRow:
    return UsageLogRow(
        id=id_, api_key_id=api_key_id, project_id=project_id, provider=provider, model=model,
        model_served=model_served, capability=capability, workflow=workflow,
        tokens_in=tokens_in, tokens_out=tokens_out, cache_read_tokens=cache_read,
        cache_write_tokens=0, cost_usd=cost, latency_ms=latency_ms, status=status,
        error_kind=error_kind, http_status=http_status, request_id=request_id,
        created_at=at or (now() - timedelta(minutes=minutes_ago)))


def job(id_: int, status: str = "pending", project_id: int = 1, minutes_ago: int = 1,
        **kw: Any) -> DeepJobRow:
    base: dict[str, Any] = {
        "id": id_, "project_id": project_id, "capability": "chat:deep", "status": status, "request": {},
        "retry_count": 0, "created_at": now() - timedelta(minutes=minutes_ago)}
    base.update(kw)
    return DeepJobRow(**base)


def audit_row(id_: int, actor: str = "dashboard", action: str = "key.added",
              target: str | None = "gemini/k1", minutes_ago: int = 1,
              metadata: dict[str, Any] | None = None) -> AuditLogRow:
    return AuditLogRow(id=id_, actor=actor, action=action, target=target,
                       metadata_=metadata or {}, ip="203.0.113.9",
                       created_at=now() - timedelta(minutes=minutes_ago))


def seed(*rows: Any) -> None:
    async def _go() -> None:
        async with get_session() as s:
            s.add_all(rows)
    run(_go())


def seed_demo() -> None:
    """Two projects, a mix of key states, 8 usage rows (incl. a 3-attempt
    fallback chain), jobs and audit rows."""
    seed(
        project(1, "stepan", daily_cost_cap_usd=1.0),
        project(2, "vera", allowed_scopes=["llm:chat", "llm:vision"]),
        key(1, "gemini", "alive-key"),
        key(2, "gemini", "dead-key", is_alive=False, last_error="401 auth failed"),
        key(3, "groq", "cooling", cooldown_until=now() + timedelta(minutes=5),
            last_error="rate limit"),
        key(4, "deepseek", "off-key", tier="paid", is_active=False),
        key(5, "cerebras", "capped", tier="free", daily_cost_cap_usd=1.0,
            daily_cost_used_usd=1.0, daily_reset_at=now().date()),
    )
    seed(
        usage(1, minutes_ago=30, cost=0.002, model="deepseek/deepseek-flash", provider="deepseek",
              capability="chat:smart", cache_read=60, tokens_in=100, api_key_id=4,
              model_served="DeepSeek-V4.1-Flash"),
        usage(2, minutes_ago=25, status="error", error_kind="TimeoutError", http_status=429,
              provider="groq", model="groq/openai/gpt-oss-120b", api_key_id=3, latency_ms=3000),
        usage(3, minutes_ago=20, project_id=2, workflow="describe", capability="vision"),
        # one request that fell through three providers (attempt N+1 starts as N ends)
        usage(10, minutes_ago=10, status="error", error_kind="TimeoutError", http_status=429,
              provider="cerebras", model="cerebras/gpt-oss-120b", api_key_id=5, latency_ms=5000,
              at=now() - timedelta(minutes=10)),
        usage(11, status="error", error_kind="RateLimitError", http_status=429, provider="groq",
              model="groq/openai/gpt-oss-120b", api_key_id=3, latency_ms=400,
              at=now() - timedelta(minutes=10) + timedelta(milliseconds=400)),
        usage(12, provider="gemini", api_key_id=1, latency_ms=1200,
              at=now() - timedelta(minutes=10) + timedelta(milliseconds=1600)),
    )
    seed(job(1, "pending", minutes_ago=45), job(2, "running", started_at=now() - timedelta(minutes=2)),
         job(3, "done", completed_at=now()), job(4, "error", error_message="all providers failed"))
    seed(audit_row(1), audit_row(2, actor="tg:1", action="login.success", target=None),
         audit_row(3, action="key.delete", target="groq/old", metadata={"why": "test"}))
