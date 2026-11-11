"""Stored login health of every subscription worker, and the verdict shown to the owner.

A worker writes only its own row (record_status). The dashboard and the monitor read all rows
(account_logins) and judge each account the same way (verdict), so the page and the Telegram
alert can never disagree. docs/subscription-logins.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from aibroker.agent.account_choices import ACCOUNT_CHOICES, CAPABILITY_PROVIDER
from aibroker.agent.accounts import ACCOUNT_DOWN_CODES, QUOTA_LOOKBACK
from aibroker.agent.job_status import STATUS_ERROR, error_code
from aibroker.agent.login_identity import APP_LABEL, LoginReport, display_name
from aibroker.db import get_session
from aibroker.db.models import AgentLoginStatusRow, AgentRunEventRow, AgentRunRow

# The worker probes every AGENT_LOGIN_PROBE_S (5 min); four missed probes = the worker is silent.
STALE_AFTER = timedelta(minutes=20)
PROVIDER_CAPABILITY = {provider: cap for cap, provider in CAPABILITY_PROVIDER.items()}

OK, LOGGED_OUT, BROKEN, STALE, UNKNOWN = "ok", "logged_out", "broken", "stale", "unknown"
DOWN_STATES = frozenset({LOGGED_OUT, BROKEN})


@dataclass(frozen=True)
class AccountLogin:
    provider: str
    account_id: str
    name: str
    app: str
    email: str | None
    plan: str | None
    state: str
    reason: str
    checked_at: datetime | None

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.account_id}"


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def verdict(row: AgentLoginStatusRow | None, down_at: datetime | None,
            ok_at: datetime | None, now: datetime) -> tuple[str, str]:
    """(state, reason). `down_at`: latest run that failed because the LOGIN cannot serve
    (ACCOUNT_DOWN_CODES); it marks the account broken only if it is newer than both the last
    successful run and the moment the login last changed (a fresh sign-in clears it)."""
    if row is None:
        return UNKNOWN, "no report from the worker yet"
    if now - row.checked_at > STALE_AFTER:
        return STALE, "the worker has not reported its login since this time"
    if not row.logged_in:
        return LOGGED_OUT, row.detail or "not logged in"
    if down_at is not None and down_at > row.changed_at and (ok_at is None or down_at > ok_at):
        return BROKEN, "logged in, but runs fail: the login cannot serve (auth refresh / access)"
    return OK, ""


async def record_status(provider: str, account_id: str, report: LoginReport,
                        now: datetime | None = None) -> None:
    """Upsert this worker's row. The last known mailbox survives a logout (a logged-out CLI
    reports none), and changed_at moves only when logged_in flips. A probe that could not ask
    the CLI (`report.failed`) writes nothing: the row ages into "stale", never "logged out"."""
    if report.failed:
        return
    now = now or _now()
    async with get_session() as s:
        row = await s.get(AgentLoginStatusRow, (provider, account_id))
        if row is None:
            s.add(AgentLoginStatusRow(
                provider=provider, account_id=account_id, logged_in=report.logged_in,
                email=report.email, plan=report.plan, detail=report.detail,
                checked_at=now, changed_at=now))
            return
        if row.logged_in != report.logged_in:
            row.changed_at = now
        row.logged_in = report.logged_in
        row.email = report.email or row.email
        row.plan = report.plan or row.plan
        row.detail = report.detail
        row.checked_at = now


async def _last_run_marks(since: datetime) -> tuple[dict[tuple[str, str], datetime],
                                                    dict[tuple[str, str], datetime]]:
    """({(provider, account): last account-down failure}, {...: last completed run})."""
    down: dict[tuple[str, str], datetime] = {}
    async with get_session() as s:
        failed = (await s.execute(
            select(AgentRunRow.capability, AgentRunRow.account_id, AgentRunEventRow.ts,
                   AgentRunEventRow.payload)
            .join(AgentRunEventRow, AgentRunEventRow.run_id == AgentRunRow.run_id)
            .where(AgentRunEventRow.type == "run.failed", AgentRunEventRow.ts >= since))).all()
        done = (await s.execute(
            select(AgentRunRow.capability, AgentRunRow.account_id, func.max(AgentRunEventRow.ts))
            .join(AgentRunEventRow, AgentRunEventRow.run_id == AgentRunRow.run_id)
            .where(AgentRunEventRow.type == "run.completed", AgentRunEventRow.ts >= since)
            .group_by(AgentRunRow.capability, AgentRunRow.account_id))).all()
    for cap, account, ts, payload in failed:
        payload = payload or {}
        code = error_code(STATUS_ERROR, payload.get("error_class"), payload.get("message"))
        key = (CAPABILITY_PROVIDER.get(cap, cap), account)
        if code in ACCOUNT_DOWN_CODES and (key not in down or ts > down[key]):
            down[key] = ts
    ok = {(CAPABILITY_PROVIDER.get(cap, cap), account): ts for cap, account, ts in done}
    return down, ok


async def account_logins(now: datetime | None = None) -> list[AccountLogin]:
    """Every supported subscription account (ACCOUNT_CHOICES), judged by verdict()."""
    now = now or _now()
    async with get_session() as s:
        rows = {(r.provider, r.account_id): r
                for r in (await s.execute(select(AgentLoginStatusRow))).scalars().all()}
    down, ok = await _last_run_marks(now - QUOTA_LOOKBACK)
    out = []
    for choice in ACCOUNT_CHOICES:
        key = (choice.provider, choice.account_id)
        row = rows.get(key)
        state, reason = verdict(row, down.get(key), ok.get(key), now)
        email = row.email if row else None
        out.append(AccountLogin(
            provider=choice.provider, account_id=choice.account_id,
            name=display_name(choice.provider, email, choice.account_id),
            app=APP_LABEL.get(choice.provider, choice.provider), email=email,
            plan=row.plan if row else None, state=state, reason=reason,
            checked_at=row.checked_at if row else None))
    return out


async def known_email(provider: str, account_id: str) -> str | None:
    """The last mailbox this login reported, to pre-fill the next sign-in page."""
    async with get_session() as s:
        row = await s.get(AgentLoginStatusRow, (provider, account_id))
        return row.email if row else None
