"""What a project spent: the same `usage_log` the cost guard sums for the project caps.

Read by `GET /v1/usage` with the project's own key, so a client shows Broker's numbers instead
of re-counting them. Subscription agent runs are flat-rate and never land in `usage_log`
(docs/agent-runs.md), so they are not part of the money totals."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select

from aibroker.db.engine import get_session
from aibroker.db.models import ProjectRow, UsageLogRow

MAX_DAYS = 366


@dataclass(frozen=True)
class Bucket:
    key: str
    calls: int
    cost_usd: float
    tokens_in: int = 0
    tokens_out: int = 0


@dataclass(frozen=True)
class ProjectUsage:
    start: date
    end: date                       # exclusive
    total: Bucket
    by_capability: list[Bucket] = field(default_factory=list)
    by_day: list[Bucket] = field(default_factory=list)
    daily_cap_usd: float | None = None
    monthly_cap_usd: float | None = None
    today_usd: float = 0.0
    month_usd: float = 0.0


def default_range(today: date) -> tuple[date, date]:
    """The current UTC calendar month up to and including today."""
    return today.replace(day=1), today + timedelta(days=1)


def check_range(start: date, end: date) -> None:
    if end <= start:
        raise ValueError("`to` must be after `from`")
    if (end - start).days > MAX_DAYS:
        raise ValueError(f"at most {MAX_DAYS} days per request")


def _at(day: date) -> datetime:
    """usage_log.created_at is naive UTC."""
    return datetime.combine(day, time.min)


async def _spent(session, project_id: int, start: date, end: date) -> float:
    q = select(func.coalesce(func.sum(UsageLogRow.cost_usd), 0.0)).where(
        UsageLogRow.project_id == project_id,
        UsageLogRow.created_at >= _at(start), UsageLogRow.created_at < _at(end),
    )
    return float((await session.execute(q)).scalar_one() or 0.0)


async def project_usage(project: ProjectRow, start: date, end: date,
                        today: date | None = None) -> ProjectUsage:
    check_range(start, end)
    today = today or datetime.now(UTC).date()
    in_range = (
        UsageLogRow.project_id == project.id,
        UsageLogRow.created_at >= _at(start), UsageLogRow.created_at < _at(end),
    )
    sums = (func.count(UsageLogRow.id), func.coalesce(func.sum(UsageLogRow.cost_usd), 0.0),
            func.coalesce(func.sum(UsageLogRow.tokens_in), 0),
            func.coalesce(func.sum(UsageLogRow.tokens_out), 0))
    day = func.date(UsageLogRow.created_at)    # date() exists in both PostgreSQL and SQLite
    async with get_session() as s:
        calls, cost, t_in, t_out = (await s.execute(select(*sums).where(*in_range))).one()
        caps = (await s.execute(
            select(UsageLogRow.capability, *sums).where(*in_range)
            .group_by(UsageLogRow.capability).order_by(func.sum(UsageLogRow.cost_usd).desc())
        )).all()
        days = (await s.execute(
            select(day, *sums).where(*in_range).group_by(day).order_by(day)
        )).all()
        today_usd = await _spent(s, project.id, today, today + timedelta(days=1))
        month_usd = await _spent(s, project.id, today.replace(day=1), today + timedelta(days=1))

    def bucket(key: object, n: int, usd: float, a: int, b: int) -> Bucket:
        return Bucket(str(key or "other"), int(n), round(float(usd), 6), int(a), int(b))

    return ProjectUsage(
        start=start, end=end,
        total=bucket("total", calls, cost, t_in, t_out),
        by_capability=[bucket(*row) for row in caps],
        by_day=[bucket(str(row[0])[:10], *row[1:]) for row in days],
        daily_cap_usd=project.daily_cost_cap_usd, monthly_cap_usd=project.monthly_cost_cap_usd,
        today_usd=round(today_usd, 6), month_usd=round(month_usd, 6),
    )
