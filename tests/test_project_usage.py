"""GET /v1/usage: a project's own spend from usage_log (the numbers the cost guard caps)."""
from __future__ import annotations

from datetime import date, datetime

from fastapi.testclient import TestClient
from sqlalchemy import update

from aibroker.db import get_session
from aibroker.db.models import ProjectRow, UsageLogRow
from aibroker.main import app
from aibroker.services.project_usage import check_range, default_range
from tests.test_routes_proxy import _make_project

client = TestClient(app)


async def _log(pid: int, when: datetime, capability: str, cost: float, tokens: int = 10) -> None:
    async with get_session() as s:
        s.add(UsageLogRow(project_id=pid, provider="gemini", model="m", capability=capability,
                          tokens_in=tokens, tokens_out=tokens, cost_usd=cost, status="ok",
                          created_at=when))


async def test_usage_needs_a_project_key():
    assert client.get("/v1/usage").status_code in (401, 403)


async def test_usage_sums_only_this_project_by_capability_and_day():
    key, pid = await _make_project(["llm:chat"])
    _, other = await _make_project(["llm:chat"])
    async with get_session() as s:
        await s.execute(update(ProjectRow).where(ProjectRow.id == pid)
                        .values(daily_cost_cap_usd=5.0, monthly_cost_cap_usd=50.0))
    await _log(pid, datetime(2026, 11, 12, 9), "speech", 0.02)
    await _log(pid, datetime(2026, 11, 12, 18), "speech", 0.01)
    await _log(pid, datetime(2026, 11, 13, 8), "chat:sales", 0.0)
    await _log(pid, datetime(2026, 10, 31, 23), "speech", 1.0)       # outside the range
    await _log(other, datetime(2026, 11, 12, 9), "speech", 9.0)      # another project

    r = client.get("/v1/usage", params={"from": "2026-11-01", "to": "2026-12-01"},
                   headers={"X-Project-Key": key})
    assert r.status_code == 200
    body = r.json()
    assert body["from"] == "2026-11-01" and body["to"] == "2026-12-01" and body["currency"] == "USD"
    assert body["total"] == {"calls": 3, "cost_usd": 0.03, "tokens_in": 30, "tokens_out": 30}
    assert body["by_capability"][0] == {"capability": "speech", "calls": 2, "cost_usd": 0.03,
                                        "tokens_in": 20, "tokens_out": 20}
    assert [(d["day"], d["calls"]) for d in body["by_day"]] == [("2026-11-12", 2), ("2026-11-13", 1)]
    assert body["limits"]["daily_cap_usd"] == 5.0 and body["limits"]["monthly_cap_usd"] == 50.0


async def test_usage_rejects_a_bad_range():
    key, _ = await _make_project(["llm:chat"])
    bad = client.get("/v1/usage", params={"from": "2026-11-10", "to": "2026-11-01"},
                     headers={"X-Project-Key": key})
    assert bad.status_code == 422


def test_default_range_is_the_current_month_and_ranges_are_bounded():
    assert default_range(date(2026, 11, 13)) == (date(2026, 11, 1), date(2026, 11, 14))
    check_range(date(2026, 1, 1), date(2026, 12, 31))
    for start, end in ((date(2026, 2, 1), date(2026, 2, 1)), (date(2025, 1, 1), date(2026, 6, 1))):
        try:
            check_range(start, end)
        except ValueError:
            continue
        raise AssertionError((start, end))
