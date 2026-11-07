"""Per-project agent access edited with checkboxes on the dashboard project Settings tab
(projects.agent_enabled / agent_models / agent_accounts, migration 026).

The checks that use it (`constants.project_allowed`, `policy.policy_for`) are synchronous and run on
every agent request and before every run, so each process keeps a snapshot of the table in memory:
both api workers and every agent worker refresh it every REFRESH_S seconds
(`access_refresh_loop`), and a dashboard save refreshes its own process at once. A change therefore
reaches every process within REFRESH_S, with no container restart.

Before the first successful snapshot load every agent policy fails closed.
Account-only overrides also replace the env allowlist and never fall back to env.
For untouched legacy projects the env settings stay a fallback: AGENT_ALLOWED_PROJECT_IDS still admits a project, and
AGENT_PROJECT_POLICY still applies to a project whose dashboard policy is empty.
After a successful load, a failed refresh keeps the last snapshot.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from aibroker.agent.account_choices import ACCOUNT_ONLY
from aibroker.db import get_session
from aibroker.db.models import ProjectRow

log = logging.getLogger(__name__)

REFRESH_S = 5.0
# One label per Claude worker container (docker-compose AGENT_ACCOUNT_ID), in the default preference
# order; Codex runs always use "owner".
KNOWN_ACCOUNTS = ("claude-2", "owner")
ALL_MODELS = "*"


@dataclass(frozen=True)
class AgentAccess:
    enabled: bool
    models: tuple[str, ...]
    accounts: tuple[str, ...]

    @property
    def account_only(self) -> bool:
        return self.models == (ACCOUNT_ONLY,)

    @property
    def has_policy(self) -> bool:
        return bool(self.models and self.accounts)


_snapshot: dict[int, AgentAccess] | None = None
POLICY_REVISION = "accounts-v1"
_evidence_pid: int | None = None
_evidence_instance: str | None = None
_evidence_generation = 0
_evidence_last_success: str | None = None


def _log_refresh(success: bool) -> None:
    """Evidence is best effort and must never affect snapshot publication or refresh errors."""
    global _evidence_pid, _evidence_instance, _evidence_generation, _evidence_last_success
    try:
        pid = os.getpid()
        if _evidence_pid != pid:
            instance = str(uuid.uuid4())
            _evidence_pid = pid
            _evidence_instance = instance
            _evidence_generation = 0
            _evidence_last_success = None
        observed_at = datetime.now(UTC).isoformat()
        if success:
            _evidence_generation += 1
            _evidence_last_success = observed_at
        payload = {
            "event": "agent_access_snapshot",
            "schema": 1,
            "policy_revision": POLICY_REVISION,
            "pid": pid,
            "process_id": _evidence_instance,
            "observed_at": observed_at,
            "snapshot_loaded": snapshot_loaded(),
            "last_refresh_ok": success,
            "generation": _evidence_generation,
            "last_success_at": _evidence_last_success,
        }
        log.log(logging.INFO if success else logging.WARNING,
                "%s", json.dumps(payload, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - telemetry cannot change access or mask the original error
        pass


def snapshot_loaded() -> bool:
    return _snapshot is not None


def access_for(project_id: int) -> AgentAccess | None:
    """The dashboard access of a project, or None when it has none set."""
    return (_snapshot or {}).get(project_id)


def _strings(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))


async def refresh_access() -> None:
    """Reload the snapshot from projects; only rows with some agent setting are kept (the table
    holds tens of projects, so it is read whole instead of filtering JSON in SQL)."""
    global _snapshot
    try:
        async with get_session() as s:
            rows = (await s.execute(
                select(ProjectRow.id, ProjectRow.agent_enabled, ProjectRow.agent_models,
                       ProjectRow.agent_accounts))).all()
        fresh = {}
        for pid, enabled, models, accounts in rows:
            access = AgentAccess(bool(enabled), _strings(models), _strings(accounts))
            if access.enabled or access.models or access.accounts:
                fresh[pid] = access
        _snapshot = fresh
    except Exception:
        _log_refresh(False)
        raise
    _log_refresh(True)


async def access_refresh_loop(stop: asyncio.Event, interval: float = REFRESH_S) -> None:
    """Refresh until `stop`; an error keeps the last snapshot and is logged."""
    while not stop.is_set():
        try:
            await refresh_access()
        except Exception:  # noqa: BLE001 - keep serving the last snapshot
            log.exception("agent access refresh failed; keeping the last snapshot")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)


def clean_models(values: Iterable[str], known: Iterable[str]) -> tuple[str, ...]:
    """Form values limited to known bare model ids and "*", in submitted order."""
    allowed = set(known) | {ALL_MODELS}
    return tuple(dict.fromkeys(v for v in values if v in allowed))


def clean_accounts(values: Iterable[str]) -> tuple[str, ...]:
    """Form values limited to KNOWN_ACCOUNTS, in KNOWN_ACCOUNTS order (the preference order)."""
    chosen = set(values)
    return tuple(a for a in KNOWN_ACCOUNTS if a in chosen)
