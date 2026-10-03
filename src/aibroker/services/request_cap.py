"""Lifetime request cap (`projects.total_request_cap`) - admission control.

One unit is spent per CLIENT request (not per provider attempt, so a fallback
walk across five providers still costs 1). Admission is a single atomic
`UPDATE ... WHERE used < cap RETURNING`: Postgres row-locks the project for the
statement, so N concurrent requests against a project with 1 request left admit
exactly one - no stale read like a Python-side compare against the ProjectRow
loaded at auth. `total_request_cap IS NULL` (every pre-existing project) is
unlimited and never writes.

A request counts once admitted, whatever the provider then does: refunding on
upstream failure would let a caller probe forever for free.
"""
from __future__ import annotations

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.db.models import ProjectRow


class RequestCapExhausted(Exception):
    """The project spent its lifetime request allowance."""

    def __init__(self, limit: int, used: int):
        self.limit = limit
        self.used = used
        super().__init__(f"request cap exhausted: {used}/{limit}")


async def admit_request(project: ProjectRow) -> None:
    """Spend one request of `project`'s lifetime allowance or raise
    RequestCapExhausted. No-op for an uncapped project. The WHERE re-reads the
    LIVE cap, so an owner raising or lifting it takes effect on the next call."""
    if project.total_request_cap is None:
        return
    async with get_session() as s:
        row = (await s.execute(
            text(
                "UPDATE projects SET total_requests_used = total_requests_used + 1 "
                "WHERE id = :id AND (total_request_cap IS NULL "
                "OR total_requests_used < total_request_cap) "
                "RETURNING total_requests_used"
            ),
            {"id": project.id},
        )).first()
        if row is not None:
            return
        live = (await s.execute(
            text("SELECT total_request_cap, total_requests_used FROM projects WHERE id = :id"),
            {"id": project.id},
        )).first()
    if live is None or live[0] is None:  # project deleted mid-request / cap lifted since
        return
    raise RequestCapExhausted(int(live[0]), int(live[1]))
